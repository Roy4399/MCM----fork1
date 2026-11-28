"""Reconcile production with modeled rail travel and buffer limits, then freeze it."""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import pairwise
from math import floor

from ortools.linear_solver import pywraplp

from model.production import operation_day_bounds, prepare, verify_candidate


class ReconciliationFailure(RuntimeError):
    # 作用：记录线性校正失败。输入：self 为异常，phase 为失败阶段，solver_status 为求解状态。返回：无。
    def __init__(self, phase, solver_status):
        super().__init__(f"Network reconciliation {phase} phase returned {solver_status}")
        self.phase = phase
        self.solver_status = solver_status


# 作用：按现有机台顺序协调工序时刻。输入：data 为计划，source 为候选排程，site 为场地；margin、extra、wait_caps 控制交接窗口，seconds 为预算，其余参数控制接续、缓存及目标顺序。返回：校正并校验后的生产排程。
def reconcile(data, source, site, margin=60, extra=None, seconds=25, continuous_by_cast_id=True,
              exposure="off", planning_buffer_order=True, targeted_buffers=(), nominal_queue="off",
              total_shift_first=False, wait_caps=None):
    epoch, stages, _, transitions, nominal, _, _, joins, ambiguous, _, _ = prepare(data)
    continuous_by_cast_id = continuous_by_cast_id and data.get("continuity_policy", "all") == "all"
    if continuous_by_cast_id:
        such_casts = {o["row"]: i for i, o in enumerate(stages) if o["kind"] == "CC"}
        joins.update((such_casts[edge["left_row"]], such_casts[edge["right_row"]]) for edge in ambiguous)
    ops = source["operations"]
    points = site["points"]
    solver = pywraplp.Solver.CreateSolver("GLOP")
    solver.SetTimeLimit(int(seconds * 1000))
    inf = solver.infinity()
    starts = []
    for i, stage in enumerate(stages):
        lo, hi = operation_day_bounds(stage, data.get("operation_day_lock"))
        starts.append(solver.NumVar(lo, inf if hi is None else hi, f"operation_{i}"))
    shifts = [solver.NumVar(0, inf, f"absolute_shift_{i}") for i in range(len(stages))]
    fixed = defaultdict(list)
    for i, (stage, op) in enumerate(zip(stages, ops, strict=True)):
        solver.Add(shifts[i] >= starts[i] - stage["target"])
        solver.Add(shifts[i] >= stage["target"] - starts[i])
        fixed[op["equipment"]].append(i)
    order = [entry["start"] for entry in ops]
    for chain in fixed.values():
        chain.sort(key=lambda i: (order[i], i))
        for a, b in pairwise(chain):
            end = starts[a] + stages[a]["duration"]
            solver.Add(starts[b] == end if (a, b) in joins else starts[b] >= end)

    inbound, outbound = defaultdict(list), defaultdict(list)
    extra = extra or {}
    reserves, excess, queue_excess = [], [], []
    for (a, b), historical in transitions.items():
        name = f'{stages[a]["row"]}:{stages[a]["kind"]}->{stages[b]["kind"]}'
        one, two = ops[a]["equipment"], ops[b]["equipment"]
        motion = abs(points[one] - points[two])
        reserve = max(historical, motion + margin) + extra.get(name, 0)
        reserves.append((a, b, reserve))
        pickup = solver.NumVar(0, inf, f"pickup_{a}_{b}")
        finish = starts[a] + stages[a]["duration"]
        solver.Add(starts[b] >= finish + reserve)
        solver.Add(pickup >= finish)
        solver.Add(pickup + motion <= starts[b])
        if wait_caps and name in wait_caps:
            solver.Add(starts[b] - finish <= max(reserve, wait_caps[name]))
        if nominal_queue != "off":
            spill = solver.NumVar(0, inf, f"handover_excess_{a}_{b}")
            solver.Add(spill >= starts[b] - finish - max(reserve, nominal[a, b]))
            queue_excess.append(spill)
        if exposure != "off" and stages[b]["kind"] == "CC":
            route = stages[a]["kind"] + "->CC"
            cap = site["casting_exposure"][route]["p95_seconds"]
            if exposure == "cap":
                solver.Add(starts[b] - finish <= cap)
            else:
                spill = solver.NumVar(0, inf, f"casting_excess_{a}_{b}")
                solver.Add(spill >= starts[b] - finish - cap)
                excess.append(spill)
        outbound[one].append((a, pickup))
        inbound[two].append((b, pickup + motion))

    capacities = site.get("buffer_capacities", {})
    for equipment, items in outbound.items():
        resource = equipment + ":out"
        if not planning_buffer_order and resource not in targeted_buffers:
            continue
        items.sort(key=lambda x: (order[x[0]], x[0]))
        k = capacities.get(resource, 1)
        for (_, pickup), (b, _) in zip(items, items[k:]):
            solver.Add(pickup <= starts[b] + stages[b]["duration"])
    for equipment, items in inbound.items():
        resource = equipment + ":in"
        if not planning_buffer_order and resource not in targeted_buffers:
            continue
        items.sort(key=lambda x: (order[x[0]], x[0]))
        k = capacities.get(resource, 1)
        for (a, _), (_, arrival) in zip(items, items[k:]):
            solver.Add(arrival >= starts[a])

    # 作用：最小化工序间超额等待并固定所得值。输入：无，使用外层求解器及超额等待变量。返回：最优超额等待，失败时抛出异常。
    def minimize_queue():
        cost = sum(queue_excess)
        solver.Minimize(cost)
        phase = solver.Solve()
        if phase != solver.OPTIMAL:
            raise ReconciliationFailure("planned handover excess", phase)
        value = floor(cost.solution_value() + 0.5)
        solver.Add(cost <= value)
        return value

    queue_value = minimize_queue() if nominal_queue == "before_cast" else None
    cast_cost = sum(d for i, d in enumerate(shifts) if stages[i]["kind"] == "CC")
    total = sum(shifts)
    if total_shift_first:
        solver.Minimize(total)
        phase = solver.Solve()
        if phase != solver.OPTIMAL:
            raise ReconciliationFailure("total-first timing", phase)
        solver.Add(total <= floor(total.solution_value()+0.5))
    solver.Minimize(cast_cost)
    phase1 = solver.Solve()
    if phase1 != solver.OPTIMAL:
        raise ReconciliationFailure("casting", phase1)
    casting = floor(cast_cost.solution_value() + 0.5)
    solver.Add(cast_cost <= casting)
    if nominal_queue == "after_cast":
        queue_value = minimize_queue()
    if excess:
        total_excess = sum(excess)
        solver.Minimize(total_excess)
        phase = solver.Solve()
        if phase != solver.OPTIMAL:
            raise ReconciliationFailure("casting exposure", phase)
        solver.Add(total_excess <= floor(total_excess.solution_value() + 0.5))
    solver.Minimize(total)
    phase2 = solver.Solve()
    if phase2 != solver.OPTIMAL:
        raise ReconciliationFailure("timing", phase2)
    selected = [floor(var.solution_value() + 0.5) for var in starts]

    # 作用：把秒数换成时间字符串。输入：second 为相对计划基准的秒数。返回：ISO 格式时间。
    def timestamp(second):
        return (epoch + timedelta(seconds=second)).isoformat(sep=" ")

    result = deepcopy(source)
    result["scope"]["operation_day_lock"] = data.get("operation_day_lock")
    for i, op in enumerate(result["operations"]):
        op.update(start=timestamp(selected[i]), end=timestamp(selected[i] + stages[i]["duration"]),
                  ordinary_shift_seconds=selected[i] - stages[i]["target"])
    for leg, (a, b, reserve) in zip(result["handover_windows"], reserves, strict=True):
        elapsed = selected[b] - selected[a] - stages[a]["duration"]
        if elapsed < reserve:
            raise ValueError("Rounding violated a transport reserve")
        leg.update(upstream_finished=timestamp(selected[b] - elapsed), downstream_starts=timestamp(selected[b]),
                   candidate_window_seconds=elapsed)
    movements = [abs(op["ordinary_shift_seconds"]) for op in result["operations"]]
    casting_actual = sum(movements[i] for i, op in enumerate(ops) if op["stage"] == "CC")
    result["network_adjustment"] = {
        "motion_margin_seconds": margin, "additional_reserves_seconds_by_task": extra,
        "fixed_pool_assignment_and_equipment_order": True, "buffer_capacities": capacities,
        "fifo_buffer_order_in_planning": planning_buffer_order,
        "targeted_buffer_order_resources": list(targeted_buffers),
        "continuous_by_cast_id": continuous_by_cast_id, "total_continuous_cast_edges": len(joins),
        "casting_exposure_policy": exposure, "casting_exposure_inputs": site["casting_exposure"],
        "nominal_handover_wait_objective": nominal_queue, "nominal_handover_excess_seconds": queue_value,
        "total_shift_first": total_shift_first, "nominal_window_caps": wait_caps or {},
        "model_scope": "single-task travel and handover buffers; fixed rail order is verified by dispatch",
        "verified_transfer_reserves_seconds": [value for _, _, value in reserves],
        "changed_operations_since_prior_plan": sum(a["start"] != b["start"] for a, b in zip(ops, result["operations"])),
    }
    result["optimization"] = {
        "casting_shift_seconds": casting_actual,
        "ordinary_shift_seconds": sum(movements),
        "ordinary_changed_operations": sum(bool(v) for v in movements),
        "maximum_operation_shift_seconds": max(movements),
        "status": "OPTIMAL_FOR_FIXED_EQUIPMENT_RECONCILIATION" if casting_actual == casting else
                  "FEASIBLE_ROUNDED_FIXED_EQUIPMENT_RECONCILIATION",
        "source_casting_shift_seconds": source["optimization"]["casting_shift_seconds"],
    }
    verify_candidate(data, result)
    return result
