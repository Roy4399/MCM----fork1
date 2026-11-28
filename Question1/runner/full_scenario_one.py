"""Run and audit full-horizon scenario one on a reproducible conditional site."""

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from model.point_dispatch import Lift, Reservations, lift_times, verify
from model.local_dispatch import reduce_wait, repair
from model.network_plan import reconcile
from model.production import read_pack, verify_candidate
from model.scenario_inputs import make_inputs
from model.scenario_inputs import pick_quantile


# 作用：选出最早且顺序一致的可行吊运。输入：proposals 为候选记录。返回：首选记录或 None。
def best(proposals):
    return min(proposals, key=lambda v: (v.pickup, v.arrival, v.car), default=None)


# 作用：按任务窗口和轨迹复算指标。输入：site 为场地，heavy、returns、feeds 为任务，state 为预约，choices 为选中的渡车任务，traces 为轨迹。返回：指标与覆盖统计。
def metrics(site, heavy, returns, feeds, state, choices, traces):
    bookings = defaultdict(list)
    jobs = [*heavy, *(j for pair in returns for j in pair), *(options[0] for options in feeds)]
    by_name = {j.name: choices.get(j.name, j) for j in jobs}
    if len(jobs) != len(by_name):
        raise ValueError("The model contains duplicate material movements")

    # 作用：登记有效的资源占用。输入：resource 为工位，a、b 为起止时刻，label 为任务名。返回：无。
    def book(resource, a, b, label):
        if resource and a < b:
            bookings[resource].append((a, b, label))

    for name, lift in state.lifts.items():
        j = by_name[name]
        if j.kind != "empty_to_ferry":
            start = j.ready - site["empirical_intervals"]["empty_pick_lag"]["seconds"] if j.kind == "empty_to_slag" else j.ready
            book(j.source, start, lift.pickup, name)
        book(j.receiver, lift.arrival, j.due, name)
        if j.kind == "empty_to_bof":
            book(lift.origin, lift.pickup - site["ferry_hold_seconds"], lift.pickup, name)
    for first, second in returns:
        a, b = state.lifts.get(first.name), state.lifts.get(second.name)
        if bool(a) != bool(b):
            raise ValueError(f"Partially assigned outbound empty ladle {first.heat}")
        if a:
            if a.arrival + second.before > b.pickup:
                raise ValueError(f"Slag processing duration violated for {first.heat}")
            book("SLAG", a.arrival, b.pickup, first.name)
            book(b.destination, b.arrival, b.arrival + site["ferry_hold_seconds"], second.name)
    if state.resources is not None:
        actual = {k: sorted(v) for k, v in state.resources.items() if v}
        if dict((k, sorted(v)) for k, v in bookings.items()) != actual:
            raise ValueError("Resource use differs from independent reconstruction")

    result = verify(site, list(by_name.values()), state.lifts, traces, bookings)
    post, waiting = Counter(), Counter()
    by_kind, receiving = defaultdict(list), defaultdict(list)
    completed = defaultdict(set)
    required = defaultdict(set)
    for name, j in by_name.items():
        required[j.row].add(name)
        if name in state.lifts:
            lift = state.lifts[name]
            completed[j.row].add(name)
            if j.kind != "empty_to_ferry":
                wait = lift.pickup - j.ready
                waiting[j.kind] += wait
                by_kind[j.kind].append(wait)
            if j.receiver:
                wait = j.due - lift.arrival
                post[j.kind] += wait
                receiving[j.receiver].append(wait)
    by_kind["empty_to_ferry"] = [
        state.lifts[b.name].pickup - state.lifts[a.name].arrival - b.before
        for a, b in returns if b.name in state.lifts]
    waiting["empty_to_ferry"] = sum(by_kind["empty_to_ferry"])
    result["wait_seconds"] = sum(waiting.values())
    all_waits = [v for values in by_kind.values() for v in values]
    result["wait_p95_seconds"] = pick_quantile(all_waits, .95) if all_waits else None
    result["wait_p95_seconds_by_kind"] = {k: pick_quantile(values, .95) for k, values in by_kind.items() if values}
    result["receiving_wait_summary_by_buffer"] = {
        k: {"n": len(v), "p95_seconds": pick_quantile(v, .95), "max_seconds": max(v)}
        for k, v in receiving.items()}
    result["wait_seconds_by_kind"] = waiting
    result["receiving_wait_seconds_by_kind"] = post
    result["assigned_by_kind"] = Counter(by_name[n].kind for n in state.lifts)
    result["tasks_by_kind"] = Counter(j.kind for j in by_name.values())
    result["fully_assigned_heats"] = sum(needed == completed[row] for row, needed in required.items())
    return result


# 作用：按规则派车并核验全周期方案。输入：site 为场地，heavy、returns、feeds 为任务，rule、rank 为排序规则，repair_seconds 为修补预算，resource_repair 控制资源优先。返回：预约、选择、轨迹、指标和未排记录。
def dispatch(site, heavy, returns, feeds, rule, rank=0, repair_seconds=0, resource_repair=False):
    state = Reservations(site)
    rejected, choices = {}, {}
    # 作用：计算最短运输后还剩多少窗口。输入：j 为吊运任务。返回：最小行程扣除后的秒数。
    def slack(j):
        return j.due - j.ready - min(sum(lift_times(site, j, x)) for x in j.destinations)

    # 作用：生成用于排队的任务排序键。输入：j 为吊运任务。返回：可比较的排序元组。
    def order(j):
        if rule == "fifo":
            return j.ready, j.due, j.name
        priorities = (
            (j.due, slack(j)),
            (j.ready + slack(j) // 2, slack(j)),
            (j.ready // 1800, slack(j)),
            (j.due // 3600, slack(j)),
        )
        return (*priorities[rank], j.ready, j.name)

    # 作用：成对插入翻渣前后空包运输。输入：first、second 为同炉前后两项空包任务。返回：无，更新预约或未排记录。
    def unload(first, second):
        lead = second.before + min(sum(lift_times(site, second, x)) for x in second.destinations)
        total_first = sum(lift_times(site, first, first.destinations[0]))
        latest = first.due - lead - total_first
        earliest = first.ready
        finish = first.ready - site["empirical_intervals"]["empty_pick_lag"]["seconds"]
        while earliest <= latest:
            first_lifts = sorted(state.proposals(first, earliest=earliest, latest=latest, hold_from=finish),
                                 key=lambda v: (v.pickup, v.car))
            for a in first_lifts:
                state.add(first, a, hold_from=finish)
                b = best(state.proposals(second, earliest=a.arrival + second.before, hold_from=a.arrival,
                                         destination_hold=site["ferry_hold_seconds"]))
                if b:
                    state.add(second, b, hold_from=a.arrival, destination_hold=site["ferry_hold_seconds"])
                    # The physical occupation includes slag processing before the second task is ready.
                    records = state.resources["SLAG"]
                    index = next(i for i, record in enumerate(records) if record[2] == second.name)
                    a0, b0, _ = records[index]
                    records[index] = (a0, b0, first.name)
                    return
                state.remove(first)
            if not first_lifts:
                break
            earliest = min(a.pickup for a in first_lifts) + 30
        rejected[first.name] = "outbound_pair_no_insertion"
        rejected[second.name] = "outbound_pair_no_insertion"

    for j in sorted(heavy, key=order):
        proposal = best(state.proposals(j))
        if proposal:
            state.add(j, proposal)
        else:
            rejected[j.name] = "isolated_window_short" if slack(j) < 0 else "reservation_insertion_failed"
    stats = repair(state, heavy, rejected, repair_seconds, prioritize_resources=resource_repair) if repair_seconds else {}
    empty_work = [(order(pair[0]), pair, True) for pair in returns]
    empty_work.extend((order(options[0]), options, False) for options in feeds)
    for _, batch, outbound in sorted(empty_work, key=lambda x: x[0]):
        if outbound:
            unload(*batch)
        else:
            proposals = [(v, j) for j in batch for v in state.proposals(
                j, departure_hold=site["ferry_hold_seconds"])]
            if proposals:
                lift, chosen = min(proposals, key=lambda pair: (
                    pair[0].pickup, pair[0].arrival, pair[0].car, pair[0].origin))
                choices[chosen.name] = chosen
                state.add(chosen, lift, departure_hold=site["ferry_hold_seconds"])
            else:
                rejected[batch[0].name] = "pre_bof_empty_insertion_failed"
    traces = state.trace()
    result = metrics(site, heavy, returns, feeds, state, choices, traces)
    result["local_repair"] = stats
    return state, choices, traces, result, rejected


# 作用：在保持覆盖的条件下提前起吊。输入：site 为场地，heavy、returns、feeds 为任务，run 为初始排班，joint_windows 为局部联排预算。返回：优化后的排班。
def improve(site, heavy, returns, feeds, run, joint_windows=0):
    """Reduce readiness-to-pickup waiting without sacrificing covered tasks."""
    state, choices, _, before, rejected = run
    groups = [*((j,) for j in heavy), *feeds]
    changes = 0
    for _ in range(3):
        moves_this_pass = 0
        groups.sort(key=lambda options: (
            -(state.lifts[options[0].name].pickup - options[0].ready) if options[0].name in state.lifts else 0,
            options[0].due, options[0].name))
        for options in groups:
            name = options[0].name
            old = state.lifts.get(name)
            if old is None or old.pickup == options[0].ready:
                continue
            original = choices.get(name, options[0])
            is_feed = original.kind == "empty_to_bof"
            hold = site["ferry_hold_seconds"] if is_feed else 0
            state.remove(original)
            candidates = [(v, j) for j in options for v in state.proposals(
                j, latest=old.pickup - 1, departure_hold=hold)]
            if candidates:
                candidate, selected = min(candidates, key=lambda item: (
                    item[0].pickup, item[0].arrival, item[0].car, item[0].origin))
                state.add(selected, candidate, departure_hold=hold)
                if is_feed:
                    choices[name] = selected
                moves_this_pass += 1
            else:
                state.add(original, old, departure_hold=hold)
        changes += moves_this_pass
        if not moves_this_pass:
            break
    traces = state.trace()
    after = metrics(site, heavy, returns, feeds, state, choices, traces)
    if after["assigned"] != before["assigned"] or after["wait_seconds"] > before["wait_seconds"]:
        raise ValueError("Wait improvement did not preserve assignment and objective")
    after["local_repair"] = before["local_repair"]
    after["successful_wait_reinsertions"] = changes
    after["original_wait_seconds"] = before["wait_seconds"]
    if joint_windows:
        stats = reduce_wait(state, heavy, joint_windows)
        traces = state.trace()
        updated = metrics(site, heavy, returns, feeds, state, choices, traces)
        if updated["assigned"] != after["assigned"] or updated["wait_seconds"] > after["wait_seconds"]:
            raise ValueError("Joint wait improvement did not preserve assignment and objective")
        for key in ("local_repair", "successful_wait_reinsertions", "original_wait_seconds"):
            updated[key] = after[key]
        updated["joint_wait_improvement"] = stats
        after = updated
    return state, choices, traces, after, rejected


# 作用：计算渡车边界的可交换钢包供需。输入：site 为场地，returns、feeds 为回收与供包，lifts 为吊运指派。返回：供需匹配及起始库存统计。
def exchange_ledger(site, returns, feeds, lifts):
    """Inventory balance with an explicit assumed outside-E–F turnaround."""
    service = site["empirical_intervals"]["outside_turnaround"]["seconds"]
    if service is None:
        return {"status": "outside replenishment is supplied at the ferry boundary",
                "minimum_starting_pool_in_model": None, "feed_source_links": []}
    arrivals = sorted((lifts[b.name].arrival + service, b.row) for _, b in returns if b.name in lifts)
    pickups = sorted((lifts[g[0].name].pickup - site["ferry_hold_seconds"], g[0].row)
                     for g in feeds if g[0].name in lifts)
    available, links = deque(), []
    carry_in = 0
    index = 0
    for at, row in pickups:
        while index < len(arrivals) and arrivals[index][0] <= at:
            available.append(arrivals[index][1])
            index += 1
        if available:
            previous = available.popleft()
        else:
            carry_in += 1
            previous = None
        links.append({"pre_bof_row": row, "released_by_outbound_row": previous})
    return {"assumed_external_service_seconds": service, "minimum_starting_pool_in_model": carry_in,
            "feed_source_links": links}


# 作用：把排班转换成可导出的记录。输入：site 为场地，heavy、returns、feeds 为任务，run 为已核验排班。返回：含明细、轨迹和指标的字典。
def emit(site, heavy, returns, feeds, run):
    state, choices, traces, result, rejected = run
    jobs = [*heavy, *(j for pair in returns for j in pair), *(options[0] for options in feeds)]
    epoch = datetime.fromisoformat(site["origin"])

    # 作用：格式化排班时间。输入：t 为场地时间原点后的秒数。返回：ISO 格式时间字符串。
    def timestamp(t):
        return (epoch + timedelta(seconds=t)).isoformat(sep=" ")

    ready_updates = {second.name: state.lifts[first.name].arrival + second.before
                     for first, second in returns if first.name in state.lifts}
    rows = []
    for default in jobs:
        j = choices.get(default.name, default)
        lift = state.lifts.get(j.name)
        record = asdict(j)
        # Internal vehicle indices follow the verified left-to-right order.
        ready = ready_updates.get(j.name, j.ready)
        record.update(ready=timestamp(ready), due=timestamp(j.due), cars=[site["order"][c] for c in j.cars])
        if lift is None:
            record.update(status="UNASSIGNED", reason=rejected[j.name])
            if j.kind == "empty_to_bof":
                record["candidate_origins"] = [option.origin for options in feeds
                                              for option in options if option.name == j.name]
        else:
            motion, service = lift_times(site, j, lift.destination)
            record.update(status="ASSIGNED", crane_id=site["order"][lift.car], pickup=timestamp(lift.pickup),
                          arrival=timestamp(lift.arrival), destination=lift.destination,
                          wait_seconds=lift.pickup - ready, motion_seconds=motion)
            if service:
                record["stationary_service_seconds"] = service
        rows.append(record)
    return {"metrics": result, "movement_tasks": rows,
            "trajectory_knots_by_crane": dict(zip(site["order"], traces)),
            "exchangeable_ladle_balance": exchange_ledger(site, returns, feeds, state.lifts)}


# 作用：原子写出 JSON。输入：path 为目标路径，result 为待写对象。返回：无。
def write_json(path, result):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    temporary.replace(path)


# 作用：从保存文件重新核对任务、轨迹和指标。输入：site 为场地，heavy、returns、feeds 为模型任务，path 为排班文件。返回：回放状态和任务覆盖统计。
def verify_saved(site, heavy, returns, feeds, path):
    """Reload exported dispatch and audit directly against rebuilt task windows."""
    saved = json.loads(path.read_text(encoding="utf8"))
    expected = {j.name: {j.origin: j} for j in [*heavy, *(j for pair in returns for j in pair)]}
    expected.update({options[0].name: {j.origin: j for j in options} for options in feeds})
    epoch = datetime.fromisoformat(site["origin"])
    positions = {crane: index for index, crane in enumerate(site["order"])}
    lifts, choices, seen = {}, {}, set()

    # 作用：恢复序列化的时间。输入：text 为排班时间字符串。返回：相对场地原点的秒数。
    def tick(text):
        return int((datetime.fromisoformat(text) - epoch).total_seconds())

    for row in saved["movement_tasks"]:
        name = row["name"]
        j = expected[name][row["origin"]]
        if name in seen or (row["row"], row["heat"], row["kind"], tick(row["due"]), row["tonnes"]) != (
                j.row, j.heat, j.kind, j.due, j.tonnes):
            raise ValueError(f"Persisted task data changed for {name}")
        seen.add(name)
        if row["status"] == "ASSIGNED":
            pickup, arrival = tick(row["pickup"]), tick(row["arrival"])
            motion, service = lift_times(site, j, row["destination"])
            if (row["motion_seconds"] != motion or row.get("stationary_service_seconds", 0) != service
                    or arrival - pickup != motion + service or row["wait_seconds"] != pickup - tick(row["ready"])):
                raise ValueError(f"Persisted duration is inconsistent for {name}")
            lifts[name] = Lift(name, positions[row["crane_id"]], row["origin"], row["destination"], pickup, arrival)
            choices[name] = j
        elif row["status"] != "UNASSIGNED":
            raise ValueError(f"Unknown disposition for {name}")
    if seen != expected.keys():
        raise ValueError("Persisted dispatch does not include every modeled task")
    slag_release = {b.name: lifts[a.name].arrival + b.before for a, b in returns if a.name in lifts}
    for row in saved["movement_tasks"]:
        j = expected[row["name"]][row["origin"]]
        if tick(row["ready"]) != slag_release.get(j.name, j.ready):
            raise ValueError(f"Persisted release time is inconsistent for {j.name}")
    traces = tuple(tuple(tuple(knot) for knot in saved["trajectory_knots_by_crane"][str(crane)])
                   for crane in site["order"])
    actual = metrics(site, heavy, returns, feeds, SimpleNamespace(lifts=lifts, resources=None), choices, traces)
    if any(saved["metrics"][key] != value for key, value in actual.items()):
        raise ValueError(f"Persisted metrics differ from independent recomputation for {path.name}")
    if saved["exchangeable_ladle_balance"] != exchange_ledger(site, returns, feeds, lifts):
        raise ValueError(f"Persisted ladle balance differs for {path.name}")
    return {"assigned": actual["assigned"], "missing": actual["missing"],
            "trajectory_knots": sum(len(one) for one in traces), "independent_file_replay_passed": True}


# 作用：统计精炼完成到开浇的区间。输入：site 为场地，production 为生产排程。返回：按路线汇总的区间统计。
def exposure_report(site, production):
    by_route = defaultdict(list)
    for leg in production["handover_windows"]:
        if leg["to_stage"] == "CC":
            by_route[leg["from_stage"] + "->CC"].append(leg["candidate_window_seconds"])
    result = {}
    for route, values in by_route.items():
        history = site["casting_exposure"][route]
        cap = history["p95_seconds"]
        result[route] = {**history, "count": len(values),
                         "count_above_p95": sum(t > cap for t in values),
                         "total_excess_seconds": sum(max(0, t - cap) for t in values),
                         "max_refining_finish_to_opening_seconds": max(values)}
    return result


# 作用：运行带实绩场地参数的排程对照。输入：无显式参数，读取命令行输入路径和参数。返回：无，写出对照结果。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--production", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scale-factor", type=float, default=1.)
    parser.add_argument("--clearance-pixels", type=int, default=20)
    parser.add_argument("--reconcile-margin", type=int)
    parser.add_argument("--repair-seconds", type=float, default=.6)
    parser.add_argument("--feedback-rounds", type=int, default=3)
    parser.add_argument("--joint-windows", type=int, default=100)
    parser.add_argument("--exposure-policy", choices=("off", "cap", "soft"), default="off")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    if not 0.25 <= args.scale_factor <= 2.0 or not 1 <= args.clearance_pixels <= 40:
        parser.error("scale factor or clearance is outside supported sensitivity settings")
    data = read_pack(args.archive)
    production = json.loads(args.production.read_text(encoding="utf-8-sig"))
    fingerprint = sha256(args.archive.read_bytes()).hexdigest()
    if production["input_sha256"] != fingerprint:
        raise ValueError("Different input archive and production plan")
    verify_candidate(data, production)
    site, heavy, returns, feeds = make_inputs(data, production, args.scale_factor, args.clearance_pixels)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    if args.audit_only:
        for name in ("baseline", "dispatch"):
            print(name, verify_saved(site, heavy, returns, feeds, output / (name + ".json")))
        return
    plan_path = args.production
    if args.reconcile_margin is not None:
        if args.reconcile_margin < 0:
            parser.error("rail reconciliation margin must be nonnegative")
        previous_production = production
        additional = {}
        for round_index in range(args.feedback_rounds + 1):
            production = reconcile(data, previous_production, site, margin=args.reconcile_margin,
                                   extra=additional, exposure=args.exposure_policy)
            site, heavy, returns, feeds = make_inputs(data, production, args.scale_factor, args.clearance_pixels)
            if round_index == args.feedback_rounds:
                break
            probe = dispatch(site, heavy, returns, feeds, "ordered_insertion", 0, args.repair_seconds)
            failed = probe[4].keys() & {job.name for job in heavy}
            if not failed:
                break
            print(f"reconciliation feedback {round_index + 1}: {', '.join(sorted(failed))}", flush=True)
            additional = additional | {name: additional.get(name, 0) + 180 for name in sorted(failed)}
        plan_path = output / "production.json"
        if plan_path.resolve() == args.production.resolve():
            parser.error("reconciliation output cannot overwrite the previous plan")
        write_json(plan_path, production)
        site, heavy, returns, feeds = make_inputs(data, production, args.scale_factor, args.clearance_pixels)
    write_json(output / "site.json", site)
    ranked = []
    for rule, rank in [("fifo", 0), *(("ordered_insertion", k) for k in range(4))]:
        started = monotonic()
        run = dispatch(site, heavy, returns, feeds, rule, rank, args.repair_seconds)
        result = run[3]
        print(f"{rule} {rank}: assigned={result['assigned']}, missing={result['missing']}, "
              f"local={result['local_repair']} ({monotonic()-started:.1f}s)", flush=True)
        if rule == "fifo":
            baseline = emit(site, heavy, returns, feeds, run)
        optimized = improve(site, heavy, returns, feeds, run)
        ranked.append((f"{rule}:{rank}", optimized))
    rank, winner = min(ranked, key=lambda item: (item[1][3]["missing"], item[1][3]["wait_seconds"],
                                                item[1][3]["empty_motion_seconds"]))
    winner = improve(site, heavy, returns, feeds, winner, joint_windows=args.joint_windows)
    final_dispatch = emit(site, heavy, returns, feeds, winner)
    write_json(output / "baseline.json", baseline)
    write_json(output / "dispatch.json", final_dispatch)
    replay = {name: verify_saved(site, heavy, returns, feeds, output / (name + ".json"))
              for name in ("baseline", "dispatch")}
    summary = {"scope": "all formal planning rows; conditional schematic geometry",
               "input_sha256": fingerprint, "production_sha256": sha256(plan_path.read_bytes()).hexdigest(),
               "baseline": baseline["metrics"], "ordered_insertion": winner[3], "selected_order": rank,
               "minimum_starting_pool_in_model": final_dispatch["exchangeable_ladle_balance"]["minimum_starting_pool_in_model"],
               "casting_exposure": exposure_report(site, production), "independent_replay": replay,
               "priority_experiments": {k: run[3] for k, run in ranked},
               "status": "ALL_TASKS_AUDITED_CONDITIONALLY" if not winner[3]["missing"] else "PARTIAL_ASSIGNMENT"}
    if not baseline["metrics"]["missing"] and not winner[3]["missing"]:
        summary["comparable_savings_seconds"] = {
            k: baseline["metrics"][k] - winner[3][k] for k in ("wait_seconds", "empty_motion_seconds")}
    write_json(output / "summary.json", summary)


if __name__ == "__main__":
    main()
