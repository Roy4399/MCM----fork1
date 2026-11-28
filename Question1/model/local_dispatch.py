"""Repair failed greedy insertions using exact event constraints in a local neighborhood."""

from bisect import bisect_left
from collections import defaultdict
from itertools import combinations

from ortools.sat.python import cp_model

from model.point_dispatch import Lift


# 作用：求允许的整数区间。输入：low、high 为边界，blocked 为禁用区间。返回：剩余可用区间。
def permitted(low, high, blocked):
    """Complement inclusive forbidden integer intervals inside a bounded domain."""
    allowed = []
    for a, b in sorted(blocked):
        if b < low:
            continue
        if a > high:
            break
        if low < a:
            allowed.append((low, min(high, a - 1)))
        low = max(low, b + 1)
        if low > high:
            break
    if low <= high:
        allowed.append((low, high))
    return allowed


# 作用：确定冲突时间带。输入：distance 为所需间隔，center 为参考时刻。返回：对应的禁用整数时间区间。
def band(distance, center):
    return (center - distance + 1, center + distance - 1) if distance > 0 else None


# 作用：计算两车锚点的时间间隔。输入：k、j 为车号，x、y 为车位，gaps 为累计安全间距。返回：两锚点所需的时间间隔。
def conflict(k, x, j, y, gaps):
    delta = (x - gaps[k]) - (y - gaps[j])
    return abs(delta) if k == j else delta if k < j else -delta


# 作用：尝试补排未分配的重包任务。输入：state 为预约状态，jobs 为任务，rejected 为未排字典，seconds 为预算，prioritize_resources 控制工位优先。返回：补排统计。
def repair(state, jobs, rejected, seconds=1.5, prioritize_resources=False):
    pending = {j.name: j for j in jobs if j.name in rejected}
    by_name = {j.name: j for j in jobs}
    stats = {"attempted": 0, "new_assignments": 0}
    for name in sorted(pending, key=lambda n: (pending[n].due, n)):
        candidate = pending[name]
        span = state.site["bounds"][1] - state.site["bounds"][0]
        resource_users = {existing for resource in (candidate.source, candidate.receiver)
                          for start, end, existing in state.resources[resource]
                          if start < candidate.due and end > candidate.ready} if prioritize_resources else set()
        for radius, limit in ((span, 13), (span * 2, 26), (span * 4, 40)):
            possible = []
            for existing, v in state.lifts.items():
                if existing in resource_users:
                    possible.append((0, 0, abs(v.pickup - candidate.ready), existing))
                    continue
                if v.pickup > candidate.due + radius and by_name[existing].ready > candidate.due:
                    continue
                if v.arrival < candidate.ready - radius and by_name[existing].due < candidate.ready:
                    continue
                touch = max(0, candidate.ready - v.arrival, v.pickup - candidate.due)
                if touch > radius:
                    if not ({candidate.source, candidate.receiver} & {
                            by_name[existing].source, by_name[existing].receiver}):
                        continue
                possible.append((1, touch, abs(v.pickup - candidate.ready), existing))
            nearby = [by_name[n] for _, _, _, n in sorted(possible)[:limit]]
            stats["attempted"] += 1
            old = [state.lifts[j.name] for j in nearby]
            for j in nearby:
                state.remove(j)
            result = solve_insertions(state, [candidate, *nearby], old, seconds)
            if result:
                for j, v in zip([candidate, *nearby], result, strict=True):
                    state.add(j, v)
                del rejected[name]
                stats["new_assignments"] += 1
                break
            for j, v in zip(nearby, old, strict=True):
                state.add(j, v)
    return stats


# 作用：联合重排相邻重包任务。输入：state 为当前预约，heavy 为重包任务，budget、seconds 为搜索预算。返回：等待优化统计。
def reduce_wait(state, heavy, budget=100, seconds=.04):
    """Jointly reschedule nearby heavy lifts while all other assignments stay fixed."""
    by_name = {j.name: j for j in heavy}
    first = sorted((v for n, v in state.lifts.items() if n in by_name),
                   key=lambda v: (by_name[v.job].ready - v.pickup, v.pickup, v.job))
    groups_seen = set()
    accepted = 0
    for focal in first:
        if len(groups_seen) >= budget:
            break
        job = by_name[focal.job]
        focal = state.lifts[job.name]
        if focal.pickup == job.ready:
            continue
        overlap = [v for n, v in state.lifts.items() if n != job.name and n in by_name
                   and v.arrival >= job.ready - 300 and v.pickup <= focal.pickup + 300]
        overlap.sort(key=lambda v: (abs(v.pickup - job.ready), v.job))
        current = [focal, *overlap[:9]]
        names = frozenset(v.job for v in current)
        if names in groups_seen or not overlap:
            continue
        groups_seen.add(names)
        group = [by_name[v.job] for v in current]
        for j in group:
            state.remove(j)
        proposed = solve_insertions(state, group, current, seconds)
        if proposed and sum(v.pickup for v in proposed) < sum(v.pickup for v in current):
            current = proposed
            accepted += 1
        for j, v in zip(group, current, strict=True):
            state.add(j, v)
    return {"windows": len(groups_seen), "improved_windows": accepted}


# 作用：求局部任务插入方案。输入：state 为固定预约，jobs 为待插任务，prior 为原有承诺，seconds 为预算。返回：可行吊运组合或 None。
def solve_insertions(state, jobs, prior, seconds):
    points, gaps = state.site["points"], state.gaps
    span = state.site["bounds"][1] - state.site["bounds"][0]
    capacity = state.site.get("buffer_capacities", {})
    model = cp_model.CpModel()
    buffer_intervals = defaultdict(list)
    entries = []
    hints = {v.job: v for v in prior}
    objective = []
    for j in jobs:
        x, y = points[j.origin], points[j.destinations[0]]
        d = abs(x - y)
        low, high = j.ready, j.due - d
        if high < low:
            return None
        start = model.new_int_var(low, high, j.name)
        end = start + d
        cars = {}
        for k in j.cars:
            if not (state.reachable(k, x) and state.reachable(k, y)):
                continue
            blocked = []
            for z, offset in ((x, 0), (y, d)):
                for other, anchors in enumerate(state.anchors):
                    idx = bisect_left(anchors, (low + offset - span,))
                    stop = bisect_left(anchors, (high + offset + span,))
                    for at, anchor_x in anchors[idx:stop]:
                        interval = band(conflict(k, z, other, anchor_x, gaps), at - offset)
                        if interval:
                            blocked.append(interval)
            crane_busy = state.loaded[k]
            idx = max(0, bisect_left(crane_busy, (low,)) - 1)
            stop = bisect_left(crane_busy, (high + d,))
            blocked.extend((a - d + 1, b - 1) for a, b, _ in crane_busy[idx:stop])
            allowed = permitted(low, high, blocked)
            if allowed:
                active = model.new_bool_var(f"{j.name}_car{k}")
                model.add_linear_expression_in_domain(start, cp_model.Domain.from_intervals(allowed)).only_enforce_if(active)
                cars[k] = active
        if not cars:
            return None
        model.add_exactly_one(cars.values())
        entries.append((start, low, high, ((x, 0), (y, d)), cars, d))
        buffer_intervals[j.source].append((j.ready, start - j.ready, start, low, high))
        buffer_intervals[j.receiver].append((end, j.due - end, j.due, low + d, j.due))
        objective.append(start - low)
        if j.name in hints:
            initial = hints[j.name]
            model.add_hint(start, initial.pickup)
            for k, var in cars.items():
                model.add_hint(var, k == initial.car)

    for a, b in combinations(entries, 2):
        sa, la, ha, ea, ca, da = a
        sb, lb, hb, eb, cb, db = b
        min_d, max_d = la - hb, ha - lb
        if min_d >= span * 2 or max_d <= -span * 2:
            continue
        for k, ck in ca.items():
            for l, cl in cb.items():
                blocked = [(-da + 1, db - 1)] if k == l else []
                for x, u in ea:
                    for y, v in eb:
                        interval = band(conflict(k, x, l, y, gaps), v - u)
                        if interval:
                            blocked.append(interval)
                allowed = permitted(min_d, max_d, blocked)
                if not allowed:
                    model.add_bool_or([~ck, ~cl])
                elif allowed != [(min_d, max_d)]:
                    model.add_linear_expression_in_domain(
                        sa - sb, cp_model.Domain.from_intervals(allowed)).only_enforce_if(ck, cl)

    for res, raw in buffer_intervals.items():
        left = min(p for a, s, b, p, q in raw)
        right = max(q for a, s, b, p, q in raw)
        intervals = []
        for idx, (a, size_expr, b, p, q) in enumerate(raw):
            size = model.new_int_var(0, q - p, f"{res}_size{idx}")
            model.add(size == size_expr)
            intervals.append(model.new_interval_var(a, size, b, f"{res}_{idx}"))
        intervals.extend(model.new_fixed_size_interval_var(max(left, a), min(right,b)-max(left,a), f"{res}_fixed{i}")
                         for i, (a, b, _) in enumerate(state.resources[res])
                         if max(left,a) < min(right,b))
        model.add_cumulative(intervals, [1] * len(intervals), capacity.get(res, 1))

    model.minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status != cp_model.FEASIBLE and status != cp_model.OPTIMAL:
        if status == cp_model.MODEL_INVALID:
            raise ValueError(solver.response_stats())
        return None
    result = []
    for j, (start, _, _, _, cars, d) in zip(jobs, entries, strict=True):
        pickup = solver.value(start)
        k = next(k for k, active in cars.items() if solver.value(active))
        result.append(Lift(j.name, k, j.origin, j.destinations[0], pickup, pickup + d))
    return result
