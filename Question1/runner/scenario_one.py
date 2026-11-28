"""Run the fixed-production scenario through planning, transport and audit."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from time import monotonic

from model.audit import audit
from model.baseline import baseline
from model.example import example
from model.map.joint import joint
from model.production import read_pack, solve, verify_candidate
from model.site import build_problem, required_inputs


# 作用：检测单任务运输窗口过紧或无法直达。输入：p 为离散调度问题。返回：不可安排的任务及原因。
def relax_transport(p):
    """Detect single-task failures even with a compatible crane waiting at pickup."""
    blockers = []
    for t in p.tasks:
        direction = 1 if t.dst > t.src else -1
        legal = [k for k in t.cars if all((x, x + direction) in p.track.edges[k]
                                          for x in range(t.src, t.dst, direction))]
        length = abs(t.dst - t.src)
        if not legal:
            reason = "no_qualified_crane_with_legal_path"
        elif t.ready + length > t.due:
            reason = "short_window"
        elif t.latest_pick is not None and t.ready > t.latest_pick:
            reason = "pickup_deadline_precedes_readiness"
        else:
            continue
        blockers.append({"task": t.name, "reason": reason, "ready_tick": t.ready,
                         "due_tick": t.due, "loaded_ticks": length, "legal_cranes": legal})
    return blockers


# 作用：用小规模模型对比各调度策略。输入：p 为调度问题，seconds 为求解预算。返回：按策略汇总的比较与审计结果。
def compare(p, seconds):
    blockers = relax_transport(p)
    if blockers:
        return {"status": "INFEASIBLE_SINGLE_TASK", "blockers": blockers}
    records = {}
    for name, algorithm in (("rule", baseline), ("joint", joint)):
        started = monotonic()
        result = algorithm(p, seconds)
        row = {"status": result.status, "elapsed_seconds": monotonic() - started,
               "task_count": len(p.tasks), "assigned_tasks": len(result.lifts)}
        if result.frames:
            row.update(metrics_ticks=audit(p, result), lifts=[asdict(v) for v in result.lifts],
                       positions_by_tick=result.frames)
        else:
            row["unscheduled_tasks"] = sorted({t.name for t in p.tasks} - {v.task for v in result.lifts})
        records[name] = row
    comparison = {"status": "AUDITED_CRANE_PLAN" if any("metrics_ticks" in x for x in records.values())
                  else "NO_AUDITED_CRANE_PLAN", "strategies": records}
    if all("metrics_ticks" in x for x in records.values()):
        a, b = (records[n]["metrics_ticks"] for n in ("rule", "joint"))
        comparison["rule_minus_joint_ticks"] = {k: a[k] - b[k] for k in ("wait", "p95", "loaded", "empty")}
        comparison["rule_minus_joint_ticks"]["movement"] = a["loaded"] + a["empty"] - b["loaded"] - b["empty"]
    return comparison


# 作用：写出 JSON。输入：path 为文件路径，value 为待写对象。返回：无。
def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 作用：运行小规模场地对照入口。输入：无显式参数，读取命令行中的计划、场地和输出设置。返回：无，打印或保存结果。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="最终清洗数据 ZIP")
    parser.add_argument("--production", type=Path, help="使用本项目计算的已核验生产候选 JSON")
    parser.add_argument("--site", type=Path, help="含轨道、工位与空包任务的现场参数 JSON")
    parser.add_argument("--demo", action="store_true", help="运行仓库随附的合成验算")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--production-seconds", default=90.0, type=float)
    parser.add_argument("--crane-seconds", default=10.0, type=float)
    args = parser.parse_args()
    if args.demo == bool(args.archive):
        parser.error("必须在 --demo 与 --archive 间二选一")
    if any(not isfinite(x) or x <= 0 for x in (args.production_seconds, args.crane_seconds)):
        parser.error("求解时间必须是正的有限秒数")
    if args.demo and (args.production or args.site):
        parser.error("--demo 仅使用仓库合成验算参数")
    for basename in ("production.json", "scenario_one.json"):
        target = (args.output_dir / basename).resolve()
        if any(p is not None and target == p.resolve() for p in (args.archive, args.production, args.site)):
            parser.error("输出路径不能覆盖输入文件")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.demo:
        report = {"case_kind": "repository_synthetic", "tick_seconds": 60, **compare(example(), args.crane_seconds)}
    else:
        fingerprint = sha256(args.archive.read_bytes()).hexdigest()
        data = read_pack(args.archive)
        if args.production:
            production = json.loads(args.production.read_text(encoding="utf-8-sig"))
            if not production["independent_production_check_passed"] or production["input_sha256"] != fingerprint:
                parser.error("候选生产计划和原输入包不匹配")
        else:
            production = solve(data, args.production_seconds)
            production["input_sha256"] = fingerprint
            write_json(args.output_dir / "production.json", production)
        verify_candidate(data, production)
        report = {"case_kind": "formal_source_data", "production_status": production["status"],
                  "production_optimization": production["optimization"], "input_sha256": fingerprint}
        if args.site:
            site = json.loads(args.site.read_text(encoding="utf-8-sig"))
            problem = build_problem(production, data, site)
            report.update(origin=site["origin"], tick_seconds=site["tick_seconds"],
                          grid_spacing_metres=site["grid_spacing_metres"],
                          crane_ids_from_left=[c["id"] for c in site["cranes"]])
            report.update(compare(problem, args.crane_seconds))
        else:
            report.update(status="SITE_INPUTS_REQUIRED", missing=required_inputs(production, data))
    write_json(args.output_dir / "scenario_one.json", report)
    print(report["status"])


if __name__ == "__main__":
    main()
