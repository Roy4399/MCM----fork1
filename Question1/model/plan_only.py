"""Build explicit planning scenarios without reading production outcomes."""

from bisect import bisect_left
from datetime import datetime, timedelta
from itertools import pairwise
from fractions import Fraction

from model.point_dispatch import Job, eligible_cranes
from model.scenario_inputs import CARS, DRAWING_POINTS, FERRIES, LIMITS, duration


# 作用：生成异常浇次的备选日期。输入：jobs 为清洗后的计划炉次。返回：日期情景列表。
def calendar_branches(jobs):
    suspect = []
    for job in jobs:
        if any(a["planned_end"] > b["planned_start"] for a, b in pairwise(job["stages"])):
            suspect.append(job)
    if len(suspect) != 1:
        raise ValueError("This documented case expects one originally reversed process handover")
    anchor = suspect[0]
    affected = sorted((j for j in jobs if (j["caster"], j["cast_id"]) ==
                       (anchor["caster"], anchor["cast_id"])), key=lambda j: j["cast_sequence"])
    if len({j["cast_sequence"] for j in affected}) != len(affected):
        raise ValueError("The affected cast has ambiguous sequence numbering")
    dates = sorted({s["planned_start"][:10] for j in affected for s in j["stages"]})
    return [{"caster": anchor["caster"], "cast_id": anchor["cast_id"], "rows": [j["source_row"] for j in affected],
             "sequence": [j["cast_sequence"] for j in affected], "anchor": date,
             "candidate_days_from_original_plan": dates,
             "basis": "conditional date of this cast, retaining time of day, duration, and unique cast sequence"}
            for date in dates]


# 作用：按计划窗口生成轨道试算参数。输入：jobs 为计划，travel_factor 为时间尺度系数，clearance_pixels 为示意图最小车距。返回：场地几何参数字典。
def geometry(jobs, travel_factor=1, clearance_pixels=20):
    west, east = 142, 1326
    bounds = []
    for job in jobs:
        for first, second in pairwise(job["stages"]):
            window = duration(first["planned_end"], second["planned_start"])
            if window <= 2:
                continue
            longest = max(abs(DRAWING_POINTS[a] - DRAWING_POINTS[b])
                          for a in first["candidates"] for b in second["candidates"])
            # Derive a test scale with headroom for one whole-rail empty reposition.
            # Reserve two seconds for rounding the two distances to integer coordinates.
            bounds.append((Fraction(window - 2, longest + east - west), job["source_row"],
                           first["kind"] + "->" + second["kind"], window, longest))
    limit, source_row, route, window, distance = min(bounds)
    scale = limit * Fraction(str(travel_factor))
    points = dict(DRAWING_POINTS)
    points.update(SLAG=round((points["CC1"] + points["CC3"]) / 2),
                  FERRY1=points["BOF_C"], FERRY2=points["BOF_B"], FERRY3=points["BOF_A"])
    projected = {key: round(pixel * scale) for key, pixel in points.items()}
    return {"points": projected, "origin": None, "order": CARS, "limits": LIMITS,
            "bounds": [round(x * scale) for x in (west, east)],
            "initial": [round(x * scale) for x in (177, 708, 818, 1110, 1252)],
            "gaps": [max(1, round(clearance_pixels * scale))] * 4, "clearance_pixels": clearance_pixels,
            "seconds_per_pixel": float(scale), "travel_factor": travel_factor, "schematic_test_points": points,
            "geometry_basis": {"name": "plan-window scenario, not measured running time",
                               "assumes_every_positive_nominal_handover_can_allow_one_full_rail_reposition": True,
                               "binding_plan_row": source_row, "binding_route": route, "binding_window_seconds": window,
                               "maximum_loaded_distance_pixels": distance, "rail_span_pixels": east - west,
                               "rounding_budget_seconds": 2, "num_positive_original_windows": len(bounds)},
            "coordinate_and_capacity_status": "CONDITIONAL_NOT_MEASURED"}


# 作用：组装指定日期的生产模型输入。输入：jobs 为炉次，rejected_rows 为保留的占位行，calendar 为日期情景，site 为场地。返回：生产求解输入字典。
def planning_data(jobs, rejected_rows, calendar, site):
    return {"jobs": jobs, "rejected_plan_rows": rejected_rows,
            "plan_only_policy": {"calendar": calendar, "points": site["points"]}}


# 作用：从冻结计划生成吊运任务。输入：production 为排程，geometry_site 为场地，buffer_capacity 为缓存容量，service_load_fraction 为服务试算比例，colocated_ferry_transfer_seconds 为原位作业时长。返回：场地、重包、空包回收及供包任务。
def make_dispatch_inputs(production, geometry_site, buffer_capacity, service_load_fraction=.5,
                         *, colocated_ferry_transfer_seconds=0):
    """Run the ferry-boundary case with explicit, plan-derived capacity stress parameters."""
    site = dict(geometry_site)
    if type(colocated_ferry_transfer_seconds) is not int or colocated_ferry_transfer_seconds < 0:
        raise ValueError("Co-located ferry service must be a non-negative integer")
    if colocated_ferry_transfer_seconds:
        site["colocated_ferry_transfer_seconds"] = colocated_ferry_transfer_seconds
    ops = production["operations"]
    cast_durations = [r["duration_seconds"] for r in ops if r["stage"] == "CC"]
    num_casters = len({r["equipment"] for r in ops if r["stage"] == "CC"})
    # A casting-cadence benchmark for the unknown duration, not an estimate of operation time.
    cadence_budget = min(cast_durations) // num_casters
    hold = int(cadence_budget * service_load_fraction)
    if hold <= 0:
        raise ValueError("ferry and slag test occupancy must exceed zero")
    first = min(datetime.fromisoformat(o["start"]) for o in ops)
    epoch = (datetime.fromisoformat(production["dispatch_origin"]) if "dispatch_origin" in production else
             first - timedelta(seconds=max(o["duration_seconds"] for o in ops if o["stage"] == "BOF") + hold))
    site.update(origin=epoch.isoformat(sep=" "), casting_exposure={},
                ferry_hold_seconds=hold,
                empirical_intervals={"empty_pick_lag": {"seconds": 0}, "outside_turnaround": {"seconds": None}},
                empty_service_case={"slag_service_seconds": hold, "ferry_occupancy_seconds": hold,
                                    "min_cast_duration_seconds": min(cast_durations), "caster_count": num_casters,
                                    "fraction_of_shared_cast_cadence_budget": service_load_fraction,
                                    "delivery_windows": "one planned casting duration after terminal cast; "
                                                        "one planned BOF duration before its start",
                                    "supply": "exchangeable serviced ladles available at the ferry boundary"},
                buffer_capacities={o["equipment"] + ":" + side: buffer_capacity for o in ops
                                   for side in ("in", "out")},
                weight_policy="unknown actual weights, 450t eligibility only, subject to measured load")
    site["buffer_capacities"].update({o["equipment"] + ":empty": buffer_capacity for o in ops if o["stage"] == "CC"})

    # 作用：转换计划时间。输入：text 为日期时刻字符串。返回：相对场地基准的秒数。
    def time(text):
        return int((datetime.fromisoformat(text) - epoch).total_seconds())

    cars = eligible_cranes(site, None)
    heavy, returns, feeds = [], [], []
    for row in production["handover_windows"]:
        origin, dest = row["from_equipment"], row["to_equipment"]
        name = f'{row["source_row"]}:{row["from_stage"]}->{row["to_stage"]}'
        heavy.append(Job(name, row["source_row"], row["heat_id"], "loaded", origin, (dest,),
                         time(row["upstream_finished"]), time(row["downstream_starts"]), cars,
                         source=origin + ":out", receiver=dest + ":in"))
    for row in ops:
        number, heat, equip = row["source_row"], row["heat_id"], row["equipment"]
        if row["stage"] == "CC":
            ready = time(row["end"])
            due = ready + row["duration_seconds"]
            slag = Job(f"{number}:CC->SLAG", number, heat, "empty_to_slag", equip, ("SLAG",), ready, due, cars,
                       source=equip + ":empty")
            ferry = Job(f"{number}:SLAG->FERRY", number, heat, "empty_to_ferry", "SLAG", FERRIES, ready, due, cars,
                        before=hold, source="SLAG")
            returns.append((slag, ferry))
        elif row["stage"] == "BOF":
            due = time(row["start"])
            feeds.append(tuple(Job(f"{number}:FERRY->BOF", number, heat, "empty_to_bof", ferry, (equip,),
                                   due - row["duration_seconds"], due, cars, receiver=equip + ":empty_in")
                               for ferry in FERRIES if (site["points"][ferry] != site["points"][equip]
                                                         or colocated_ferry_transfer_seconds)))
    return site, heavy, returns, feeds


# 作用：保持起落时刻并通过换车减少空驶。输入：state 为当前预约，heavy 为重包任务，traces 为轨迹，max_evaluations 为试算次数。返回：改进轨迹和搜索统计。
def reduce_empty_motion(state, heavy, traces, max_evaluations):
    """Try alternate cranes at the same lift times, accepting only measured motion savings."""
    points = state.site["points"]

    # 作用：累计轨迹的水平行驶量。输入：points_by_car 为各车轨迹折点。返回：等效行驶秒数。
    def motion(points_by_car):
        return sum(abs(a[1] - b[1]) for car in points_by_car for a, b in pairwise(car))

    # 作用：估算插入任务导致的额外空驶。输入：car 为内部车号，index 为插入位置，source、destination 为起落坐标。返回：增量等效秒数。
    def added_distance(car, index, source, destination):
        queue = state.loaded[car]
        before = (points[state.lifts[queue[index - 1][2]].destination] if index else state.site["initial"][car])
        after = points[state.lifts[queue[index][2]].origin] if index < len(queue) else None
        return abs(before - source) + (abs(destination - after) - abs(before - after) if after is not None else 0)

    trials = []
    for job in heavy:
        old = state.lifts.get(job.name)
        if old is None:
            continue
        source, destination = points[old.origin], points[old.destination]
        queue = state.loaded[old.car]
        index = bisect_left(queue, (old.pickup,))
        # Marginal deadhead savings from removing the task, without shared-rail effects.
        before = points[state.lifts[queue[index-1][2]].destination] if index else state.site["initial"][old.car]
        after = points[state.lifts[queue[index+1][2]].origin] if index + 1 < len(queue) else None
        removal = abs(before - source) + (abs(destination - after) - abs(before - after) if after is not None else 0)
        for car in job.cars:
            if car == old.car:
                continue
            idx = bisect_left(state.loaded[car], (old.pickup,))
            gain = removal - added_distance(car, idx, source, destination)
            if gain > 0:
                trials.append((-gain, job.name, car, job))
    incumbent = motion(traces)
    attempts, accepted = 0, 0
    for _, _, car, job in sorted(trials):
        if attempts == max_evaluations:
            break
        previous = state.lifts[job.name]
        if previous.car == car:
            continue
        state.remove(job)
        alternative = next((v for v in state.proposals(job, earliest=previous.pickup, latest=previous.pickup)
                            if v.car == car), None)
        if alternative:
            state.add(job, alternative)
            path = state.trace()
            value = motion(path)
            attempts += 1
            if value < incumbent:
                incumbent, traces = value, path
                accepted += 1
                continue
            state.remove(job)
        state.add(job, previous)
    return traces, {"evaluated_safe_reassignments": attempts, "accepted": accepted}
