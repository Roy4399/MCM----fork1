"""Prospective scenario-one experiment using nominal plan rows exclusively."""

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic

from ortools.linear_solver import pywraplp

from model.network_plan import ReconciliationFailure, reconcile
from model.plan_only import calendar_branches, geometry, make_dispatch_inputs, planning_data, reduce_empty_motion
from model.production import ProductionSearchFailure, read_pack, solve, verify_candidate
from model.scenario_inputs import duration
from runner.full_scenario_one import dispatch, emit, metrics, verify_saved, write_json


# 作用：比较派车方案优先级。输入：run 为包含预约和指标的派车结果。返回：按未排、等待、空驶组成的排序键。
def rank(run):
    m = run[3]
    return m["missing"], m["wait_seconds"] + sum(m["receiving_wait_seconds_by_kind"].values()), m["empty_motion_seconds"]


# 作用：为未排重包生成额外行车提前量。输入：site 为场地，heavy 为重包任务，attempts 为试排结果，tested 为已试任务。返回：新增余量及触发记录。
def rail_feedback(site, heavy, attempts, tested):
    """Generate targeted, geometry-derived readiness buffers on failed heavy lifts."""
    rejected = attempts[4]
    added = {}
    span = site["bounds"][1] - site["bounds"][0]
    witnesses = []
    for job in heavy:
        if job.name not in rejected or job.name in tested:
            continue
        length = abs(site["points"][job.origin] - site["points"][job.destinations[0]])
        # The full corridor is an upper bound on one reposition, conditional on no blocking.
        # A subset of these reserves can therefore be tested without imposing them on unrelated heats.
        headroom = span - max(0, job.due - job.ready - length)
        if headroom <= 0:
            continue
        added[job.name] = span
        witnesses.append({"task": job.name, "tested_window_seconds": job.due - job.ready,
                          "loaded_motion_seconds": length, "empty_reposition_bound_seconds": span,
                          "additional_reserve_seconds": span,
                          "minimum_added_over_current_slack_for_this_test": headroom,
                          "reason": rejected[job.name]})
    return added, witnesses


# 作用：定位导致任务未排的饱和工位。输入：heavy 为重包任务，attempts 为试排结果。返回：任务与饱和时段清单。
def saturated_feedback(heavy, attempts):
    """Identify resources that are saturated inside a failed heavy task's window."""
    state, _, _, _, missed = attempts
    findings = []
    for job in heavy:
        if job.name not in missed:
            continue
        for resource in (job.source, job.receiver):
            saturated = [(a, b) for a, b, _ in state.saturated(resource) if a < job.due and b > job.ready]
            if saturated:
                findings.append({"task": job.name, "resource": resource, "full_intervals_ticks": saturated})
    return findings


# 作用：从清洗计划生成全周期结果。输入：无显式参数，读取命令行计划、配置及输出目录。返回：无，写出各场景结果。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--plan-input", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--production-seconds", type=float, default=15)
    parser.add_argument("--repair-seconds", type=float, default=.4)
    parser.add_argument("--travel-factor", type=float, default=1)
    parser.add_argument("--service-load-fraction", type=float, default=.5)
    parser.add_argument("--buffer-capacity", type=int, default=2)
    parser.add_argument("--colocated-ferry-transfer-seconds", type=int, default=0,
                        help="Positive assumed handling duration enables zero-horizontal-distance ferry transfers")
    parser.add_argument("--clearance-pixels", type=int, default=20)
    parser.add_argument("--calendar-anchor")
    parser.add_argument("--lock-dates", choices=("all", "starts", "casting", "production"),
                        help="Lock all occupied operation dates, all start dates, both casting dates, or opening day")
    parser.add_argument("--continuity", choices=("all", "confirmed"), default="all")
    parser.add_argument("--warm-start-root", type=Path,
                        help="Previous schedules used only as hints, never as calendar constraints")
    parser.add_argument("--feedback-rounds", type=int, default=2)
    parser.add_argument("--motion-search", type=int, default=24,
                        help="Number of verified empty-motion reassignment evaluations")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--heavy-reposition-reserve", action="store_true",
                        help="Model one full-rail empty reposition as a planning reserve")
    args = parser.parse_args()
    if not (0 < args.travel_factor and 0 < args.service_load_fraction <= 1
            and args.buffer_capacity > 0 and args.clearance_pixels > 0
            and args.colocated_ferry_transfer_seconds >= 0
            and args.production_seconds > 0 and args.repair_seconds > 0
            and args.motion_search >= 0 and args.feedback_rounds >= 0):
        parser.error("Scenario settings must be positive and capacity stress must be no more than one")
    if args.archive:
        package = read_pack(args.archive)
        # No history, measured weights, or historical time intervals enter either model.
        nominal = {key: package[key] for key in ("jobs", "rejected_plan_rows")}
        del package
    else:
        nominal = json.loads(args.plan_input.read_text(encoding="utf-8-sig"))
        if set(nominal) != {"jobs", "rejected_plan_rows"}:
            parser.error("--plan-input accepts only the sanitized plan and placeholder lists")
    jobs, rejected_rows = nominal["jobs"], nominal["rejected_plan_rows"]
    fingerprint = sha256(json.dumps(nominal, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    base = geometry(jobs, args.travel_factor, args.clearance_pixels)

    # 作用：为同一轮试算重建任务。输入：已冻结的生产计划。返回：场地、重包、空包回收和供包任务。
    def dispatch_inputs(plan):
        return make_dispatch_inputs(plan, base, args.buffer_capacity, args.service_load_fraction,
                                    colocated_ferry_transfer_seconds=args.colocated_ferry_transfer_seconds)

    branches = calendar_branches(jobs)
    if args.calendar_anchor:
        branches = [branch for branch in branches if branch["anchor"] == args.calendar_anchor]
        if not branches:
            parser.error("Calendar anchor must be one of the original planned dates")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.audit_only and args.archive:
        write_json(args.output_dir / "plan_input.json", nominal)
    results = []
    for branch in branches:
        started = monotonic()
        target = args.output_dir / branch["anchor"]
        target.mkdir(exist_ok=True)
        data = planning_data(jobs, rejected_rows, branch, base)
        if args.lock_dates:
            data["operation_day_lock"] = args.lock_dates
        data["continuity_policy"] = args.continuity
        if args.audit_only:
            frozen = json.loads((target / "production.json").read_text(encoding="utf8"))
            verify_candidate(data, frozen)
            site, heavy, returns, feeds = dispatch_inputs(frozen)
            for name in ("baseline", "dispatch"):
                print(branch["anchor"], name, verify_saved(site, heavy, returns, feeds, target / (name + ".json")))
            continue
        hints = None
        if args.warm_start_root:
            path = args.warm_start_root / branch["anchor"] / "production.json"
            previous = json.loads(path.read_text(encoding="utf8"))
            verify_candidate(data, previous)
            hints = [op["start"] for op in previous["operations"]]
        try:
            initial = solve(data, args.production_seconds, warm_starts=hints)
        except ProductionSearchFailure as error:
            row = {"calendar": branch, "operation_day_lock": args.lock_dates,
                   "status": "PRODUCTION_SEARCH_FAILED", "detail": str(error)}
            write_json(target / "summary.json", row)
            results.append(row)
            print(branch["anchor"], row["status"], row["detail"], flush=True)
            continue
        assumed_site, *_ = dispatch_inputs(initial)
        reserve = base["bounds"][1] - base["bounds"][0] if args.heavy_reposition_reserve else 0
        extra, decisions = {}, []
        keep_buffer_order = True
        try:
            for round_index in range(args.feedback_rounds + 1):
                try:
                    frozen = reconcile(data, initial, assumed_site, margin=reserve, extra=extra, exposure="off",
                                       planning_buffer_order=keep_buffer_order)
                except ReconciliationFailure as error:
                    if (not keep_buffer_order or error.phase != "casting"
                            or error.solver_status != pywraplp.Solver.INFEASIBLE):
                        raise
                    keep_buffer_order = False
                    frozen = reconcile(data, initial, assumed_site, margin=reserve, extra=extra, exposure="off",
                                       planning_buffer_order=False)
                site, heavy, returns, feeds = dispatch_inputs(frozen)
                probe = dispatch(site, heavy, returns, feeds, "ordered_insertion", 0, args.repair_seconds)
                if not probe[3]["missing"] or round_index == args.feedback_rounds:
                    break
                added, witnesses = rail_feedback(site, heavy, probe, extra)
                if not added:
                    break
                extra.update(added)
                decisions.append(witnesses)
                print(branch["anchor"], f"feedback {round_index+1}", sorted(added), flush=True)
        except ReconciliationFailure as error:
            results.append({"calendar": branch, "operation_day_lock": args.lock_dates,
                            "status": "NO_FIXED_MACHINE_BUFFER_RECONCILIATION", "detail": str(error)})
            write_json(target / "summary.json", results[-1])
            print(branch["anchor"], results[-1], flush=True)
            continue
        baseline = dispatch(site, heavy, returns, feeds, "fifo", repair_seconds=args.repair_seconds)
        ranked = [probe, *(dispatch(site, heavy, returns, feeds, "ordered_insertion", k, args.repair_seconds)
                           for k in range(1, 4))]
        winner = min([baseline, *ranked], key=rank)
        targeted_feedback = []
        if winner[3]["missing"] and not keep_buffer_order:
            targeted_feedback = saturated_feedback(heavy, baseline)
            resources = sorted({item["resource"] for item in targeted_feedback})
            if resources:
                try:
                    refined = reconcile(data, frozen, site, margin=reserve, extra=extra, exposure="off",
                                        planning_buffer_order=False, targeted_buffers=resources)
                except ReconciliationFailure as error:
                    print(branch["anchor"], "targeted buffer correction", str(error), flush=True)
                else:
                    candidate_site, ch, cr, cf = dispatch_inputs(refined)
                    repair_budget = max(1., args.repair_seconds)
                    base_run = dispatch(candidate_site, ch, cr, cf, "fifo",
                                        repair_seconds=repair_budget, resource_repair=True)
                    candidate_run = dispatch(candidate_site, ch, cr, cf, "ordered_insertion",
                                             repair_seconds=repair_budget, resource_repair=True)
                    better = min((base_run, candidate_run), key=rank)
                    if better[3]["missing"] < winner[3]["missing"]:
                        frozen, site, heavy, returns, feeds = refined, candidate_site, ch, cr, cf
                        baseline, winner = base_run, better
                        print(branch["anchor"], "targeted buffer correction", resources,
                              "remaining", winner[3]["missing"], flush=True)
        if not winner[3]["missing"] and args.motion_search:
            state, choices, trace, initial_metrics, rejected = deepcopy(winner)
            trace, stats = reduce_empty_motion(state, heavy, trace, args.motion_search)
            m = metrics(site, heavy, returns, feeds, state, choices, trace)
            if rank((None, None, None, m)) > rank(winner):
                raise ValueError("Empty-motion search worsened the lexicographic objective")
            m.update(local_repair=initial_metrics["local_repair"], empty_motion_search=stats)
            winner = state, choices, trace, m, rejected
        write_json(target / "production.json", frozen)
        write_json(target / "site.json", site)
        summaries = {}
        for filename, run in (("baseline", baseline), ("dispatch", winner)):
            path = target / f"{filename}.json"
            write_json(path, emit(site, heavy, returns, feeds, run))
            summaries[filename] = {"metrics": run[3], "replay": verify_saved(site, heavy, returns, feeds, path)}
        row = {"calendar": branch, "calendar_reference_changes": frozen["date_correction"]["reference_changes"],
               "production": frozen["optimization"], "status": "ALL_ASSIGNED_CONDITIONALLY" if not winner[3]["missing"]
               else "PARTIAL_ASSIGNMENT", "results": summaries,
               "geometry": base["geometry_basis"], "seconds_per_pixel": base["seconds_per_pixel"],
               "service_assumptions": site["empty_service_case"], "buffer_capacity": args.buffer_capacity,
               "planning_motion_margin_seconds": reserve, "feedback_evidence": decisions,
               "fifo_buffer_order_in_production_model": keep_buffer_order,
               "targeted_buffer_feedback": targeted_feedback,
               "operation_day_lock": args.lock_dates,
               "total_shift_vs_original_including_calendar_seconds":
                   sum(abs(duration(o["original_start"], o["start"])) for o in frozen["operations"]),
               "plan_input_sha256": fingerprint}
        write_json(target / "summary.json", row)
        results.append(row)
        print(branch["anchor"], row["status"],
              [(k, v["metrics"]["missing"], v["metrics"]["assigned_by_kind"]) for k, v in summaries.items()],
              f"{monotonic()-started:.1f}s", flush=True)
    if not args.audit_only:
        write_json(args.output_dir / "comparison.json", results)


if __name__ == "__main__":
    main()
