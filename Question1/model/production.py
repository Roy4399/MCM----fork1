"""Generate a deterministic production baseline before the crane solver."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
from math import ceil, floor, isfinite
import heapq
import json
from pathlib import Path
from statistics import median
from time import monotonic
from zipfile import ZipFile

from ortools.sat.python import cp_model


# 作用：读取生产输入包。输入：path 为压缩包路径。返回：包内生产输入字典。
def read_pack(path):
    with ZipFile(path) as bundle:
        return json.loads(bundle.read('inputs_cleaned_zip_only.json'))


# 作用：汇总路线运输时间样本。输入：data 为生产输入。返回：按工艺路线区分的运输余量字典。
def route_buffers(data):
    """Use a documented empirical travel allowance, leaving the rest of each window free."""
    observed = defaultdict(list)
    for sample in data['transport_samples']:
        duration = int((datetime.fromisoformat(sample['arrival'])
                        - datetime.fromisoformat(sample['pickup'])).total_seconds())
        if duration > 0:
            observed[sample['route']].append(duration)
    return {route: {'seconds': ceil(median(values)), 'sample_count': len(values)}
            for route, values in sorted(observed.items())}


# 作用：将计划转换为求解结构并检查输入。输入：data 为计划及情景设置。返回：基准、工序、工艺边、设备组和日期校正等模型数据。
def prepare(data):
    jobs = data['jobs']
    plan_policy = data.get('plan_only_policy')
    epoch = min(datetime.fromisoformat(s['planned_start']) for j in jobs for s in j['stages'])
    if data.get('operation_day_lock'):
        epoch = epoch.replace(hour=0, minute=0, second=0, microsecond=0)
    stages, routes, correction = [], [], None
    if plan_policy:
        correction = {'status': 'UNCONFIRMED_SCENARIO', 'scenario': plan_policy['calendar'],
                      'reference_changes': []}

    # 作用：计算基准时间偏移。输入：text 为工序日期时刻。返回：相对基准的整数秒。
    def second(text):
        return int((datetime.fromisoformat(text) - epoch).total_seconds())

    for job in jobs:
        route = []
        for source in job['stages']:
            kind = source['kind']
            raw = second(source['planned_start']), second(source['planned_end'])
            start, end = raw
            if plan_policy and job['source_row'] in plan_policy['calendar']['rows']:
                reference_day = datetime.fromisoformat(plan_policy['calendar']['anchor'])
                difference = (reference_day.date() - datetime.fromisoformat(source['planned_start']).date()).days
                start, end = start + difference * 86400, end + difference * 86400
                if difference:
                    correction['reference_changes'].append({
                        'source_row': job['source_row'], 'stage': kind,
                        'original_start': source['planned_start'], 'original_end': source['planned_end'],
                        'calendar_shift_seconds': difference * 86400,
                        'date_corrected_start': (epoch + timedelta(seconds=start)).isoformat(sep=' '),
                        'date_corrected_end': (epoch + timedelta(seconds=end)).isoformat(sep=' ')})
            elif not plan_policy and (job['source_row'], job['heat_id'], kind) == (208, '26BD6255', 'BOF'):
                matched = [x for x in data['history'] if x['heat_id'] == job['heat_id']]
                actual = matched[0]['events']['tapping_end'] if len(matched) == 1 else None
                if source['planned_start'] != '2026-09-28 14:58:56' or actual != '2026-09-27 15:30:10':
                    raise ValueError('The evidence for the documented date correction changed')
                start, end = start - 86400, end - 86400
                correction = {'source_row': 208, 'heat_id': job['heat_id'], 'stage': 'BOF',
                              'original_start': source['planned_start'], 'original_end': source['planned_end'],
                              'actual_tapping_end': actual, 'historical_source_row': matched[0]['source_row'],
                              'calendar_shift_seconds': -86400,
                              'date_corrected_start': (epoch + timedelta(seconds=start)).isoformat(sep=' '),
                              'date_corrected_end': (epoch + timedelta(seconds=end)).isoformat(sep=' ')}
            if end <= start or start < 0:
                raise ValueError(f'Invalid stage in row {job["source_row"]}')
            route.append(len(stages))
            stages.append({'row': job['source_row'], 'heat': job['heat_id'], 'kind': kind,
                           'equipment': tuple(source['candidates']), 'target': start, 'duration': end-start,
                           'raw_start': source['planned_start'], 'raw_end': source['planned_end'],
                           'cast_id': job['cast_id'], 'seq': job['cast_sequence'],
                           'starts_new_cast': job['replace_tundish'] or job['rethread']})
        routes.append(route)
    if correction is None:
        raise ValueError('The documented date correction is missing')
    allowances = {} if plan_policy else route_buffers(data)
    transitions, nominal_gaps = {}, {}
    for route in routes:
        for left, right in zip(route, route[1:]):
            before, after = stages[left], stages[right]
            gap = after['target'] - before['target'] - before['duration']
            if gap < 0:
                raise ValueError(f'Unresolved negative nominal window at row {before["row"]}')
            route_key = f'{before["kind"]}->{after["kind"]}'
            if plan_policy:
                points = plan_policy['points']
                travel = min(abs(points[a] - points[b]) for a in before['equipment'] for b in after['equipment'])
                transitions[left, right] = travel
                allowance = allowances.setdefault(route_key, {'minimum_seconds': travel, 'maximum_seconds': travel})
                allowance['minimum_seconds'] = min(allowance['minimum_seconds'], travel)
                allowance['maximum_seconds'] = max(allowance['maximum_seconds'], travel)
            else:
                if route_key not in allowances:
                    raise ValueError(f'No documented transport sample for {route_key}')
                transitions[left, right] = allowances[route_key]['seconds']
            nominal_gaps[left, right] = gap

    fixed, pools = defaultdict(list), defaultdict(list)
    for i, s in enumerate(stages):
        group = fixed if s['kind'] in ('BOF', 'CC') else pools
        group[s['equipment']].append(i)
    for items in fixed.values():
        items.sort(key=lambda i: (stages[i]['target'], stages[i]['row']))
    if plan_policy:
        rows = set(plan_policy['calendar']['rows'])
        chain = fixed[(plan_policy['calendar']['caster'],)]
        linked = sorted((i for i in chain if stages[i]['row'] in rows), key=lambda i: stages[i]['seq'])
        index = min(chain.index(i) for i in linked)
        chain[:] = [i for i in chain if stages[i]['row'] not in rows]
        chain[index:index] = linked
    for kind in ('LF', 'RH'):
        compatible_sets = {s['equipment'] for s in stages if s['kind'] == kind}
        if len(compatible_sets) != 1 or len(next(iter(compatible_sets))) != 2:
            raise ValueError(f'Expected a common two-machine pool for {kind}')

    joins, ambiguous = set(), []
    for equipment, items in fixed.items():
        if not equipment[0].startswith('CC'):
            continue
        for a, b in zip(items, items[1:]):
            left, right = stages[a], stages[b]
            if left['cast_id'] != right['cast_id'] or right['starts_new_cast']:
                continue
            if right['seq'] == left['seq'] + 1:
                joins.add((a, b))
            else:
                ambiguous.append({'caster': equipment[0], 'cast_id': left['cast_id'],
                                  'left_row': left['row'], 'right_row': right['row'],
                                  'left_sequence': left['seq'], 'right_sequence': right['seq']})
    return epoch, stages, routes, transitions, nominal_gaps, fixed, pools, joins, ambiguous, correction, allowances


# 作用：确定工序允许开始的日期范围。输入：stage 为工序，mode 为日期锁定方式。返回：开始时刻的上下界。
def operation_day_bounds(stage, mode):
    """Preserve occupied calendar days; the finish of a half-open interval may be midnight."""
    if not mode or (mode in ("casting", "production") and stage["kind"] != "CC"):
        return 0, None
    start, duration = stage["target"], stage["duration"]
    first = start // 86400 * 86400
    if mode in ("production", "starts"):
        return first, first + 86399
    last = (start + duration - 1) // 86400 * 86400
    return max(first, last + 1 - duration), min(first + 86399, last + 86400 - duration)


# 作用：把相邻同浇次的候选接续纳入模型。输入：stages 为工序，joins 为接续集合，ambiguous 为待确认接续。返回：无，原地扩展 joins。
def extend_continuity(stages, joins, ambiguous):
    cast = {s['row']: i for i,s in enumerate(stages) if s['kind'] == 'CC'}
    joins.update((cast[e["left_row"]], cast[e["right_row"]]) for e in ambiguous)


class ProductionSearchFailure(RuntimeError):
    pass


# 作用：构造生产求解提示。输入：stages、routes 为工序及路线，transitions 为运输间隔，fixed、pools 为机台约束，joins 为接续。返回：工序起始时刻提示。
def feasible_hint(stages, routes, transitions, fixed, pools, joins):
    hint = {}
    for equipment, chain in fixed.items():
        if not equipment[0].startswith('BOF'):
            continue
        end = 0
        for item in chain:
            op = stages[item]
            hint[item] = max(end, op['target'])
            end = hint[item] + op['duration']

    ready = []
    successors = {a: b for route in routes for a, b in zip(route, route[1:])}
    predecessors = {b: a for a, b in successors.items()}
    free = {equipment: [0] * len(equipment) for equipment in pools}

    # 作用：汇总工序可开始的下界。输入：item 为当前工序索引。返回：最早开工秒数。
    def release(item):
        next_item = successors[item]
        if stages[next_item]['kind'] != 'CC':
            at = hint[item] + stages[item]['duration'] + transitions[item, next_item]
            heapq.heappush(ready, (max(at, stages[next_item]['target']), next_item))

    for route in routes:
        release(route[0])
    while ready:
        at, item = heapq.heappop(ready)
        op = stages[item]
        available = free[op['equipment']]
        machine = min(range(len(available)), key=available.__getitem__)
        hint[item] = max(at, available[machine])
        available[machine] = hint[item] + op['duration']
        release(item)

    for equipment, chain in fixed.items():
        if not equipment[0].startswith('CC'):
            continue
        blocks = []
        for item in chain:
            if blocks and (blocks[-1][-1], item) in joins:
                blocks[-1].append(item)
            else:
                blocks.append([item])
        end = 0
        for block in blocks:
            offset, start = 0, max(end, stages[block[0]]['target'])
            for item in block:
                prev = predecessors[item]
                ready_time = hint[prev] + stages[prev]['duration'] + transitions[prev, item]
                start = max(start, ready_time - offset)
                offset += stages[item]['duration']
            end = start
            for item in block:
                hint[item] = end
                end += stages[item]['duration']
    return hint


# 作用：检查指定时刻并分配机台。输入：stages、starts 为工序及起始秒数，fixed、pools 为机台约束，transitions、joins 为交接及接续约束。返回：工序到机台的映射。
def allocate(stages, starts, fixed, pools, transitions, joins):
    assigned = {}
    for equipment, chain in fixed.items():
        for a, b in zip(chain, chain[1:]):
            end = starts[a] + stages[a]['duration']
            if end > starts[b] or ((a, b) in joins and end != starts[b]):
                raise ValueError(f'Invalid machine sequence at rows {stages[a]["row"]}, {stages[b]["row"]}')
        assigned.update((i, equipment[0]) for i in chain)
    for equipment, chain in pools.items():
        free = {machine: 0 for machine in equipment}
        for i in sorted(chain, key=lambda item: (starts[item], item)):
            machine = min(equipment, key=free.__getitem__)
            if free[machine] > starts[i]:
                raise ValueError(f'Equipment capacity exceeded at row {stages[i]["row"]}')
            assigned[i] = machine
            free[machine] = starts[i] + stages[i]['duration']
    for (a, b), gap in transitions.items():
        if starts[a] + stages[a]['duration'] + gap > starts[b]:
            raise ValueError(f'Handover window violated at row {stages[a]["row"]}')
    if len(assigned) != len(stages):
        raise ValueError('Some operations have no equipment assignment')
    return assigned


# 作用：独立核对生产候选排程。输入：data 为计划，result 为候选排程。返回：无，不满足约束时抛出异常。
def verify_candidate(data, result):
    """Rebuild the planning inputs and check a candidate before freezing it for dispatch."""
    epoch, stages, _, transitions, nominal, fixed, pools, joins, ambiguous, correction, _ = prepare(data)
    entries = result["operations"]
    if len(entries) != len(stages) or result["date_correction"] != correction:
        raise ValueError("生产候选工序数量或日期校正与本次输入不符")
    if result["scope"]["unconfirmed_cast_edges"] != ambiguous:
        raise ValueError("待确认炉序清单与原输入不符")
    day_lock = data.get("operation_day_lock")
    if result["scope"].get("operation_day_lock") != day_lock:
        raise ValueError("生产排程的日期锁定口径不一致")
    starts = {}
    for i, (row, op) in enumerate(zip(entries, stages, strict=True)):
        elapsed = (datetime.fromisoformat(row["start"]) - epoch).total_seconds()
        if elapsed < 0 or elapsed != int(elapsed):
            raise ValueError(f'原表第 {op["row"]} 行候选时间超出允许范围')
        starts[i] = int(elapsed)
        low, high = operation_day_bounds(op, day_lock)
        if high is not None and not low <= starts[i] <= high:
            raise ValueError(f'原表第 {op["row"]} 行 {op["kind"]} 工序跨越已锁定日期')
        if ((row["source_row"], row["heat_id"], row["stage"]) != (op["row"], op["heat"], op["kind"])
                or (row["original_start"], row["original_end"]) != (op["raw_start"], op["raw_end"])
                or datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"]) != timedelta(seconds=op["duration"])
                or row["duration_seconds"] != op["duration"]
                or row["ordinary_shift_seconds"] != starts[i] - op["target"]):
            raise ValueError(f'原表第 {op["row"]} 行候选的标识、加工时长或偏移量有误')
    if (day_lock and data.get("continuity_policy", "all") == "all") or result.get("network_adjustment", {}).get("continuous_by_cast_id"):
        extend_continuity(stages, joins, ambiguous)
    allocated = allocate(stages, starts, fixed, pools, transitions, joins)
    if result.get("network_adjustment"):
        machine_stages = defaultdict(list)
        for i, row in enumerate(entries):
            if row["equipment"] not in stages[i]["equipment"]:
                raise ValueError("校正后工序的机台超出原计划范围")
            machine_stages[row["equipment"]].append(i)
        for chain in machine_stages.values():
            chain.sort(key=lambda i: starts[i])
            for a, b in zip(chain, chain[1:]):
                if starts[a] + stages[a]["duration"] > starts[b]:
                    raise ValueError("校正后指定机台仍有工序重叠")
    elif any(row["equipment"] != allocated[i] for i, row in enumerate(entries)):
        raise ValueError("候选计划的机台分配与核验结果不符")
    if len(result["handover_windows"]) != len(transitions):
        raise ValueError("缺少完整的重包交接计划")
    for row, ((a, b), allowance) in zip(result["handover_windows"], transitions.items(), strict=True):
        before, after = entries[a], entries[b]
        expected = {"source_row": before["source_row"], "heat_id": before["heat_id"],
                    "from_stage": before["stage"], "to_stage": after["stage"],
                    "from_equipment": before["equipment"], "to_equipment": after["equipment"],
                    "upstream_finished": before["end"], "downstream_starts": after["start"],
                    "reference_nominal_seconds": nominal[a, b],
                    ("minimum_modeled_travel_seconds" if data.get("plan_only_policy") else
                     "empirical_transport_allowance_seconds"): allowance,
                    "candidate_window_seconds": starts[b] - starts[a] - stages[a]["duration"]}
        if row != expected:
            raise ValueError(f'原表第 {before["source_row"]} 行重包交接与候选工序时间不符')


# 作用：度量与原计划相比的时刻改动。输入：stages 为计划工序，starts 为候选起始时刻。返回：连铸偏差和总偏差。
def objective(stages, starts):
    shifts = [abs(starts[i] - s['target']) for i, s in enumerate(stages)]
    return sum(d for d, s in zip(shifts, stages, strict=True) if s['kind'] == 'CC'), sum(shifts)


# 作用：在固定设备顺序下优化起始时刻。输入：stages 为工序，seed 为候选起始时刻，fixed、pools 为机台组，transitions、joins 为交接及接续，seconds 为预算，day_lock 为锁日方式。返回：校正起始时刻。
def polish(stages, seed, fixed, pools, transitions, joins, seconds, day_lock=None):
    """Improve timing with the seed's equipment sequences held fixed."""
    from ortools.linear_solver import pywraplp

    assigned = allocate(stages, seed, fixed, pools, transitions, joins)
    machine_orders = defaultdict(list)
    for i in sorted(seed, key=lambda i: (seed[i], i)):
        machine_orders[assigned[i]].append(i)
    solver = pywraplp.Solver.CreateSolver('GLOP')
    deadline = monotonic() + seconds
    starts = []
    for i in range(len(stages)):
        lo, hi = operation_day_bounds(stages[i], day_lock)
        starts.append(solver.NumVar(lo, solver.infinity() if hi is None else hi, f'time_{i}'))
    changes = [solver.NumVar(0, solver.infinity(), f'offset_{i}') for i in seed]
    for s, t, d in zip(stages, starts, changes, strict=True):
        solver.Add(d >= t - s['target'])
        solver.Add(d >= s['target'] - t)
    for chain in machine_orders.values():
        for a, b in zip(chain, chain[1:]):
            end = starts[a] + stages[a]['duration']
            solver.Add(starts[b] == end if (a, b) in joins else starts[b] >= end)
    for (a, b), gap in transitions.items():
        solver.Add(starts[b] >= starts[a] + stages[a]['duration'] + gap)

    # 作用：求解给定目标并保留改进。输入：cost 为线性目标，incumbent 为原候选。返回：当前阶段采用的起始时刻。
    def run(cost, incumbent):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return incumbent
        solver.SetTimeLimit(max(1, int(remaining * 1000)))
        solver.Minimize(cost)
        if solver.Solve() != pywraplp.Solver.OPTIMAL:
            return incumbent
        candidate = {i: floor(t.solution_value() + 0.5) for i, t in enumerate(starts)}
        try:
            allocate(stages, candidate, fixed, pools, transitions, joins)
        except ValueError:
            return incumbent
        return min((candidate, incumbent), key=lambda v: objective(stages, v))

    casting_cost = sum(changes[i] for i, op in enumerate(stages) if op['kind'] == 'CC')
    refined = run(casting_cost, seed)
    solver.Add(casting_cost <= objective(stages, refined)[0])
    return run(sum(changes), refined)


# 作用：计算连铸时刻偏差下界。输入：stages 为工序，fixed 为固定设备组，joins 为接续，horizon 为时域，seconds 为预算。返回：连铸目标下界。
def casting_lower_bound(stages, fixed, joins, horizon, seconds):
    """Relax other machines to prove a lower bound on the casting objective."""
    model = cp_model.CpModel()
    differences = []
    for chain in fixed.values():
        if stages[chain[0]]['kind'] != 'CC':
            continue
        starts = {}
        for i in chain:
            op = stages[i]
            starts[i] = model.new_int_var(0, horizon - op['duration'], f'cast_{i}')
            delta = model.new_int_var(0, horizon, f'delta_{i}')
            model.add_abs_equality(delta, starts[i] - op['target'])
            differences.append(delta)
        for a, b in zip(chain, chain[1:]):
            end = starts[a] + stages[a]['duration']
            model.add(starts[b] == end if (a, b) in joins else starts[b] >= end)
    model.minimize(sum(differences))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    status = solver.solve(model)
    return int(solver.best_objective_bound) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0


# 作用：排定生产工序并核验候选方案。输入：data 为清洗计划，seconds 为预算，warm_starts 为可选旧计划提示。返回：带工序和重包交接窗口的结果字典。
def solve(data, seconds, warm_starts=None):
    epoch, stages, routes, transitions, nominal_gaps, fixed, pools, joins, ambiguous, correction, allowances = prepare(data)
    day_lock = data.get("operation_day_lock")
    if day_lock and data.get("continuity_policy", "all") == "all":
        extend_continuity(stages, joins, ambiguous)
    day_bounds = [operation_day_bounds(op, day_lock) for op in stages]
    hint = feasible_hint(stages, routes, transitions, fixed, pools, joins)
    allocate(stages, hint, fixed, pools, transitions, joins)
    deadline = monotonic() + seconds
    hint_feasible = all(high is None or low <= hint[i] <= high for i,(low,high) in enumerate(day_bounds))
    if hint_feasible:
        hint = polish(stages, hint, fixed, pools, transitions, joins, min(8, seconds * 0.2), day_lock)
    # The finite domain includes the constructive feasible solution and a bound
    # derived from serializing every operation and protected handover.
    horizon = max(s['target'] for s in stages) + sum(s['duration'] for s in stages) + sum(transitions.values())
    lower_bound = casting_lower_bound(stages, fixed, joins, horizon, min(5, seconds * 0.1))
    model = cp_model.CpModel()
    starts, costs = [], []
    for i, op in enumerate(stages):
        low, high = day_bounds[i]
        high = horizon - op['duration'] if high is None else high
        t = model.new_int_var(low, high, f'start_{i}')
        d = model.new_int_var(0, horizon, f'deviation_{i}')
        model.add_abs_equality(d, t - op['target'])
        suggested = hint[i] if warm_starts is None else int((datetime.fromisoformat(warm_starts[i]) - epoch).total_seconds())
        suggested = max(low, min(high, suggested))
        model.add_hint(t, suggested)
        model.add_hint(d, abs(suggested - op['target']))
        starts.append(t)
        costs.append(d)
    for chain in fixed.values():
        for a, b in zip(chain, chain[1:]):
            end = starts[a] + stages[a]['duration']
            model.add(starts[b] == end if (a, b) in joins else starts[b] >= end)
    for (a, b), gap in transitions.items():
        model.add(starts[b] >= starts[a] + stages[a]['duration'] + gap)
    for pool, chain in pools.items():
        intervals = [model.new_fixed_size_interval_var(starts[i], stages[i]['duration'], f'interval_{i}') for i in chain]
        model.add_cumulative(intervals, [1] * len(chain), len(pool))
    casting_cost = sum(costs[i] for i, s in enumerate(stages) if s['kind'] == 'CC')
    total_cost = sum(costs)
    hint_casting, adjustment_budget = objective(stages, hint)
    # While looking for an improved candidate, do not trade a casting improvement
    # for an arbitrarily large overall shift. Optimality is relative to this budget.
    if hint_feasible:
        model.add(total_cost <= adjustment_budget)
    model.add(casting_cost >= lower_bound)
    model.minimize(casting_cost)
    first = cp_model.CpSolver()
    first.parameters.num_search_workers = 4
    chosen, status, first_bound = hint if hint_feasible else None, cp_model.UNKNOWN, lower_bound
    if hint_feasible and hint_casting == lower_bound:
        status = cp_model.OPTIMAL
    elif deadline > monotonic():
        first.parameters.max_time_in_seconds = max(0.01, (deadline - monotonic()) * 0.6)
        status = first.solve(model)
        if status in (cp_model.MODEL_INVALID, cp_model.INFEASIBLE):
            raise ProductionSearchFailure(f'Production CP-SAT status {first.status_name(status)}')
        first_bound = max(lower_bound, first.best_objective_bound)
        if status != cp_model.UNKNOWN:
            candidate = {i: first.value(v) for i, v in enumerate(starts)}
            chosen = min((candidate, hint), key=lambda v: objective(stages, v)) if hint_feasible else candidate
    if chosen is None:
        raise ProductionSearchFailure(f'Production CP-SAT status {first.status_name(status)}')
    if deadline > monotonic() and chosen is not hint:
        candidate = polish(stages, chosen, fixed, pools, transitions, joins,
                           min(5, (deadline - monotonic()) * 0.3), day_lock)
        if not hint_feasible or objective(stages, candidate)[1] <= adjustment_budget:
            chosen = candidate
    chosen_casting_cost = objective(stages, chosen)[0]
    casting_proven = chosen_casting_cost == lower_bound
    second_status = None
    second_bound = None
    if deadline > monotonic():
        model.add(casting_cost == chosen_casting_cost)
        model.minimize(total_cost)
        model.clear_hints()
        for i, s in enumerate(stages):
            model.add_hint(starts[i], chosen[i])
            model.add_hint(costs[i], abs(chosen[i] - s['target']))
        second = cp_model.CpSolver()
        second.parameters.max_time_in_seconds = max(0.01, deadline - monotonic())
        second.parameters.num_search_workers = 4
        phase = second.solve(model)
        second_status = second.status_name(phase)
        if phase in (cp_model.MODEL_INVALID, cp_model.INFEASIBLE):
            raise RuntimeError(f'Known feasible hint contradicted solver status {second_status}')
        if phase in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            candidate = {i: second.value(v) for i, v in enumerate(starts)}
            chosen = min((chosen, candidate), key=lambda v: objective(stages, v))
            second_bound = second.best_objective_bound
    assigned = allocate(stages, chosen, fixed, pools, transitions, joins)

    # 作用：把模型时刻换成日期时间。输入：t 为相对计划基准的秒数。返回：ISO 格式时间。
    def timestamp(t):
        return (epoch + timedelta(seconds=t)).isoformat(sep=' ')

    result = []
    for i, s in enumerate(stages):
        result.append({'source_row': s['row'], 'heat_id': s['heat'], 'stage': s['kind'], 'equipment': assigned[i],
                       'original_start': s['raw_start'], 'original_end': s['raw_end'],
                       'adjustment_reference_start': timestamp(s['target']), 'start': timestamp(chosen[i]),
                       'end': timestamp(chosen[i] + s['duration']), 'duration_seconds': s['duration'],
                       'ordinary_shift_seconds': chosen[i] - s['target']})
    handovers = [{'source_row': stages[a]['row'], 'heat_id': stages[a]['heat'],
                  'from_stage': stages[a]['kind'], 'to_stage': stages[b]['kind'],
                  'from_equipment': assigned[a], 'to_equipment': assigned[b],
                  'upstream_finished': timestamp(chosen[a] + stages[a]['duration']),
                  'downstream_starts': timestamp(chosen[b]),
                  'reference_nominal_seconds': nominal_gaps[a, b],
                  ('minimum_modeled_travel_seconds' if data.get('plan_only_policy') else
                   'empirical_transport_allowance_seconds'): gap,
                  'candidate_window_seconds': chosen[b] - chosen[a] - stages[a]['duration']}
                 for (a, b), gap in transitions.items()]
    return {'status': 'PRODUCTION_CANDIDATE', 'independent_production_check_passed': True,
            'scope': {'formal_heats': len(routes), 'operations': len(stages), 'handovers': len(handovers),
                      'placeholder_rows_not_assumed_to_be_bookings': len(data['rejected_plan_rows']),
                      'protected_continuous_cast_edges': len(joins), 'unconfirmed_cast_edges': ambiguous,
                      'transfer_budget_basis': ('conditional minimum schematic motion over eligible endpoints'
                                                if data.get('plan_only_policy') else
                                                'ceil median of positive historical pickup-to-drop seconds by process route'),
                      'transfer_allowances': allowances,
                      'calibrated_pickup_preparation_or_receiving_lags': False,
                      'fixed_machine_order_basis': 'date-corrected planned start order on each BOF and caster',
                      'operation_day_lock': day_lock},
            'date_correction': correction,
            'optimization': {'casting_status': first.status_name(cp_model.OPTIMAL if casting_proven else status),
                             'casting_relaxation_lower_bound_seconds': lower_bound,
                             'casting_best_bound_within_search_cap_seconds': first_bound,
                             'casting_optimal_under_model': casting_proven,
                             'casting_shift_seconds': chosen_casting_cost, 'ordinary_shift_status': second_status,
                             'ordinary_shift_bound_seconds_at_fixed_casting_cost': second_bound,
                             'total_shift_search_cap_seconds': adjustment_budget if hint_feasible else None,
                             'lexicographic_optimal_within_search_cap': (casting_proven or status == cp_model.OPTIMAL)
                                                                        and second_status == 'OPTIMAL',
                             'ordinary_shift_seconds': sum(abs(op['ordinary_shift_seconds']) for op in result),
                             'ordinary_changed_operations': sum(op['ordinary_shift_seconds'] != 0 for op in result)},
            'operations': result, 'handover_windows': handovers}


# 作用：运行生产排程命令行。输入：无显式参数，读取命令行文件路径和求解预算。返回：无，写出候选生产计划。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seconds', type=float, default=45.0)
    args = parser.parse_args()
    if args.archive.resolve() == args.output.resolve() or not isfinite(args.seconds) or args.seconds <= 0:
        parser.error('Provide a separate output file and a positive finite solver budget')
    original_hash = sha256(args.archive.read_bytes()).hexdigest()
    result = solve(read_pack(args.archive), args.seconds)
    if sha256(args.archive.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError('Source archive changed during computation')
    result['input_sha256'] = original_hash
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(result['scope'] | {'unconfirmed_cast_edges': len(result['scope']['unconfirmed_cast_edges'])})
    print(result['optimization'])


if __name__ == '__main__':
    main()
