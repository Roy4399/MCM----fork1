"""Package and independently verify day-locked schedule candidates."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path

from model.plan_only import calendar_branches, geometry, make_dispatch_inputs, planning_data, reduce_empty_motion
from model.point_dispatch import Lift, Reservations
from model.production import verify_candidate
from runner.full_scenario_one import emit, metrics, verify_saved, write_json


# 作用：读取 JSON 文件。输入：path 为文件路径。返回：解析后的对象。
def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# 作用：核对生产日和场地并重建任务。输入：nominal 为清洗计划，production 为生产排程，site 为场地。返回：日期情景、重包、空包回收和供包任务。
def system(nominal, production, site):
    jobs = nominal["jobs"]
    branch, = (item for item in calendar_branches(jobs)
               if item == production["date_correction"]["scenario"])
    g = geometry(jobs, site["travel_factor"], site["clearance_pixels"])
    data = planning_data(jobs, nominal["rejected_plan_rows"], branch, g)
    data["operation_day_lock"] = production["scope"]["operation_day_lock"]
    if data["operation_day_lock"] != "production":
        raise ValueError("Delivery requires the planned opening-day constraint")
    if not production["network_adjustment"]["continuous_by_cast_id"]:
        data["continuity_policy"] = "confirmed"
    verify_candidate(data, production)
    capacity = max(site["buffer_capacities"].values())
    regenerated, heavy, returns, feeds = make_dispatch_inputs(
        production, g, capacity, site["empty_service_case"]["fraction_of_shared_cast_cadence_budget"],
        colocated_ferry_transfer_seconds=site.get("colocated_ferry_transfer_seconds", 0))
    if any(tuple(regenerated[key]) != tuple(site[key]) for key in ("order", "limits")):
        raise ValueError("Saved crane IDs, order or tonnage limits differ from the documented fleet")
    if any(regenerated[key] != site[key] for key in (
        "points", "bounds", "gaps", "initial", "origin", "geometry_basis", "empty_service_case", "ferry_hold_seconds"
    )):
        raise ValueError("Geometry or day-boundary service parameters differ from the documented model")
    if regenerated["buffer_capacities"].keys() != site["buffer_capacities"].keys():
        raise ValueError("Site resources do not match the documented model")
    return branch, heavy, returns, feeds


# 作用：从已通过审计的方案重建行车预约。输入：site 为场地，heavy、returns、feeds 为三类任务，path 为方案文件。返回：预约状态及任务选择。
def rehydrate(site, heavy, returns, feeds, path):
    """Restore the already independently verified seed with its complete reservations."""
    raw = read(path)
    origin = datetime.fromisoformat(site["origin"])
    holds = site["ferry_hold_seconds"]
    fleet = list(site["order"])
    lift = {}
    for row in raw["movement_tasks"]:
        if row["status"] != "ASSIGNED":
            continue
        ready, arrival = (int((datetime.fromisoformat(row[key])-origin).total_seconds())
                          for key in ("pickup","arrival"))
        lift[row["name"]] = Lift(row["name"],fleet.index(row["crane_id"]),row["origin"],
                                 row["destination"],ready,arrival)
    state = Reservations(site)
    choices = {}
    for j in heavy:
        if j.name in lift:
            state.add(j,lift[j.name])
    for a,b in returns:
        if a.name in lift:
            one,two = lift[a.name],lift[b.name]
            state.add(a,one,hold_from=a.ready)
            state.add(b,two,destination_hold=holds,hold_from=one.arrival)
            records = state.resources["SLAG"]
            i = next(i for i,row in enumerate(records) if row[2]==b.name)
            x,y,_ = records[i]
            records[i] = (x,y,a.name)
    for options in feeds:
        name = options[0].name
        if name in lift:
            chosen = next(j for j in options if j.origin==lift[name].origin)
            choices[name] = chosen
            state.add(chosen,lift[name],departure_hold=holds)
    return state,choices


# 作用：逐炉核对开浇日期并统计当日任务覆盖。输入：nominal 为原计划，branch 为日期情景，production 为生产排程，schedule 为吊运方案。返回：逐日审计字典。
def audit_days(nominal, branch, production, schedule):
    """Check each confirmed heat's opening day against the original input."""
    group = set(branch["rows"])
    expected = {j["source_row"]: (branch["anchor"] if j["source_row"] in group else j["stages"][-1]["planned_start"][:10])
                for j in nominal["jobs"]}
    actual = {x["source_row"]: x["start"][:10] for x in production["operations"] if x["stage"]=="CC"}
    if actual != expected:
        raise ValueError("A heat opened on an unauthorized day")
    stage_cross = Counter(x["stage"] for x in production["operations"] if (
        x["stage"]!="CC" and x["source_row"] not in group and x["start"][:10]!=x["original_start"][:10]))
    joints = defaultdict(Counter)
    for task in schedule["movement_tasks"]:
        d = actual[task["row"]]
        joints[d]["required"] += 1
        joints[d]["assigned"] += task["status"]=="ASSIGNED"
    confirmed = Counter(day for row,day in expected.items() if row not in group)
    assumed = Counter(day for row,day in expected.items() if row in group)
    first, last = [datetime.fromisoformat(d) for d in (min(expected.values()),max(expected.values()))]
    by_day = {}
    for i in range((last-first).days+1):
        day = (first+timedelta(days=i)).date().isoformat()
        by_day[day] = {"confirmed_heats":confirmed[day],"exceptional_heats_assumed":assumed[day],
                       "required_lifts":joints[day]["required"],"assigned_lifts":joints[day]["assigned"]}
    return {"confirmed_heats":len(expected)-len(group), "exceptional_heats":len(group),
            "unauthorized_opening_day_changes":0,"other_known_stage_start_date_changes":stage_cross,
            "classification":"Counts per heat's opening day, including its transports across midnight",
            "by_day":by_day}


# 作用：以保存方案为起点试换行车并生成对照。输入：nominal 为原计划，source、dest 为源和输出目录，args 为搜索配置。返回：排班对照摘要。
def deliver(nominal, source, dest, args):
    p, s = read(source/"production.json"),read(source/"site.json")
    branch, h, r, f = system(nominal,p,s)
    dest.mkdir(parents=True,exist_ok=True)
    path = source/"dispatch.json"
    seed = read(path)
    verify_saved(s,h,r,f,path)
    state,choices = rehydrate(s,h,r,f,path)
    # Reduce the hypothetical capacity envelope to that actually used by the
    # independently verified seed, with two as the base for paired buffers.
    peaks = seed["metrics"]["max_occupancy_by_resource"]
    if seed["metrics"]["missing"] == 0:
        s["buffer_capacities"] = {k:max(2,peaks.get(k,0)) for k in s["buffer_capacities"]}
    state.site = s
    trace = state.trace()
    base = metrics(s,h,r,f,state,choices,trace)
    misses = {v["name"]:v["reason"] for v in seed["movement_tasks"] if v["status"]=="UNASSIGNED"}
    write_json(dest/"production.json",p)
    write_json(dest/"site.json",s)
    write_json(dest/"baseline.json",emit(s,h,r,f,(state,choices,trace,base,misses)))
    stats = {}
    if not misses:
        trace,stats = reduce_empty_motion(state,h,trace,args.motion_evaluations)
    m = metrics(s,h,r,f,state,choices,trace)
    if (m["wait_seconds"],m["receiving_wait_seconds_by_kind"]) != (base["wait_seconds"],base["receiving_wait_seconds_by_kind"]):
        raise ValueError("Motion optimization changed waiting")
    if m["empty_motion_seconds"] > base["empty_motion_seconds"]:
        raise ValueError("Motion optimization increased distance")
    final = emit(s,h,r,f,(state,choices,trace,m,misses))
    write_json(dest/"dispatch.json",final)
    report = {"calendar":branch,"dates":audit_days(nominal,branch,p,final),"metrics":m,
              "baseline_metrics":base,"motion_search":stats,"unscheduled":misses,
              "empty_motion_saved_seconds":base["empty_motion_seconds"]-m["empty_motion_seconds"],
              "buffer_above_two":{k:v for k,v in s["buffer_capacities"].items() if v>2},
              "replay": {name:verify_saved(s,h,r,f,dest/(name+".json")) for name in ("baseline","dispatch")}}
    write_json(dest/"summary.json",report)
    return report


# 作用：执行全周期结果交付或回放。输入：无显式参数，配置从命令行读取。返回：无，打印回放或写出结果。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-input",type=Path,required=True)
    parser.add_argument("--source",type=Path)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--motion-evaluations",type=int,default=45)
    parser.add_argument("--audit-only",action="store_true")
    args = parser.parse_args()
    if args.audit_only:
        p,s = read(args.output_dir/"production.json"),read(args.output_dir/"site.json")
        nominal = read(args.plan_input)
        branch,h,r,f = system(nominal,p,s)
        for name in ("baseline","dispatch"):
            file = args.output_dir/(name+".json")
            check = verify_saved(s,h,r,f,file)
            audit_days(nominal,branch,p,read(file))
            print(branch["anchor"],name,check,flush=True)
    else:
        if args.source is None or args.source.resolve() == args.output_dir.resolve():
            parser.error("Provide separate source and output paths")
        q = deliver(read(args.plan_input),args.source,args.output_dir,args)
        print(q["calendar"]["anchor"],q["metrics"]["assigned"],q["metrics"]["missing"],
              q["empty_motion_saved_seconds"],q["buffer_above_two"],flush=True)


if __name__ == "__main__":
    main()
