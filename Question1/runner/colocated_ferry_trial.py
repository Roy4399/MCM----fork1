"""Try co-located ferry transfers on a saved, independently verified day-locked schedule."""

import argparse
from itertools import pairwise
from pathlib import Path

from model.point_dispatch import lift_times
from runner.day_lock_delivery import audit_days, read, rehydrate, system
from runner.full_scenario_one import emit, metrics, verify_saved, write_json


# 作用：统计水平行驶量。输入：全体行车的轨迹折点。返回：等效行驶秒数，不含原位作业。
def horizontal_motion(traces):
    return sum(abs(x - y) for trace in traces for (_, x), (_, y) in pairwise(trace))


# 作用：在原排班上逐段试换同坐标渡车，保留覆盖并降低总车时。输入：已核验状态、备选任务、轨迹、评价预算。
# 返回：新轨迹与试算统计；状态和渡车选择原地更新。
def try_colocated(state, choices, feeds, trace, budget):
    site = state.site
    hold = site["ferry_hold_seconds"]
    service = site["colocated_ferry_transfer_seconds"]
    points = site["points"]
    cases = []
    for options in feeds:
        name = options[0].name
        previous = state.lifts[name]
        length, _ = lift_times(site, choices[name], previous.destination)
        if length > service:
            target = next(job for job in options if points[job.origin] == points[job.destinations[0]])
            cases.append((service - length, name, target))

    current_motion = horizontal_motion(trace)
    accepted, evaluated = [], 0
    for _, name, target in sorted(cases):
        if evaluated >= budget:
            break
        old_job = choices[name]
        old = state.lifts[name]
        old_motion, _ = lift_times(site, old_job, old.destination)
        state.remove(old_job)
        options = list(state.proposals(target, earliest=old.pickup, latest=old.pickup,
                                       departure_hold=hold))
        best = None
        for lift in options:
            if evaluated >= budget:
                break
            state.add(target, lift, departure_hold=hold)
            proposed_trace = state.trace()
            motion = horizontal_motion(proposed_trace)
            evaluated += 1
            state.remove(target)
            # Empty mileage must not increase; total horizontal travel plus handling must fall.
            if motion <= current_motion - old_motion and motion + service < current_motion:
                key = (motion, lift.car)
                if best is None or key < best[0]:
                    best = (key, lift, proposed_trace)
        if best is None:
            state.add(old_job, old, departure_hold=hold)
        else:
            (current_motion, _), lift, trace = best
            state.add(target, lift, departure_hold=hold)
            choices[name] = target
            accepted.append({"task": name, "from": old.origin, "to": target.origin,
                             "previous_crane": site["order"][old.car], "crane": site["order"][lift.car]})
    return trace, {"candidates": len(cases), "evaluated": evaluated, "accepted": accepted}


# 作用：重放一套旧结果，并输出同生产计划和同起吊时刻的试算对照。
# 输入：计划输入字典、旧结果目录、新结果目录、原位交接秒数、评价次数。返回：对照摘要。
def run(nominal, source, destination, service_seconds, budget):
    production, site = read(source / "production.json"), read(source / "site.json")
    branch, heavy, returns, feeds = system(nominal, production, site)
    path = source / "dispatch.json"
    if verify_saved(site, heavy, returns, feeds, path)["missing"]:
        raise ValueError("The comparison needs a complete saved schedule")
    if site.get("colocated_ferry_transfer_seconds"):
        raise ValueError("Use an original schedule without co-located transfers as the reference")
    site["colocated_ferry_transfer_seconds"] = service_seconds
    branch, heavy, returns, feeds = system(nominal, production, site)
    state, choices = rehydrate(site, heavy, returns, feeds, path)
    trace = state.trace()
    before = metrics(site, heavy, returns, feeds, state, choices, trace)
    baseline = emit(site, heavy, returns, feeds, (state, choices, trace, before, {}))
    trace, search = try_colocated(state, choices, feeds, trace, budget)
    after = metrics(site, heavy, returns, feeds, state, choices, trace)
    final = emit(site, heavy, returns, feeds, (state, choices, trace, after, {}))
    if (after["missing"] or any(a["pickup"] != b["pickup"]
                               for a, b in zip(baseline["movement_tasks"], final["movement_tasks"], strict=True))):
        raise ValueError("The comparison changed task coverage or pickup times")
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in (("production", production), ("site", site), ("baseline", baseline), ("dispatch", final)):
        write_json(destination / (name + ".json"), value)
    fields = ("assigned", "missing", "empty_motion_seconds", "loaded_motion_seconds",
              "stationary_service_seconds", "wait_seconds", "receiving_wait_seconds_by_kind")
    report = {
        "calendar": branch, "colocated_ferry_transfer_seconds": service_seconds,
        "assumption": "trial value, not a measured lifting/placement time",
        "evaluation_budget": budget, "search": search,
        "before": {key: before[key] for key in fields}, "after": {key: after[key] for key in fields},
        "dates": audit_days(nominal, branch, production, final),
        "replay": {name: verify_saved(site, heavy, returns, feeds, destination / (name + ".json"))
                   for name in ("baseline", "dispatch")},
    }
    write_json(destination / "summary.json", report)
    return report


# 作用：运行同坐标渡车的敏感性试算。输入：无显式参数，读取命令行中的旧排班、时长和预算。返回：无，写出对照并打印摘要。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-input", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--service-seconds", type=int, default=60)
    parser.add_argument("--evaluations", type=int, default=45)
    args = parser.parse_args()
    if args.service_seconds <= 0 or args.evaluations < 0 or args.source.resolve() == args.output_dir.resolve():
        parser.error("Use positive service time, a non-negative budget and a separate output directory")
    result = run(read(args.plan_input), args.source, args.output_dir, args.service_seconds, args.evaluations)
    print(result["calendar"]["anchor"], len(result["search"]["accepted"]),
          result["before"], result["after"], flush=True)


if __name__ == "__main__":
    main()
