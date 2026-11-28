"""Prepare a reproducible travel-time surrogate from the schematic and cleaned data."""

from collections import defaultdict
from datetime import datetime, timedelta
from math import ceil, floor

from model.point_dispatch import Job, eligible_cranes
from model.site import available_weights


# Horizontal coordinates read from the 1420 px reproduction of the question diagram.
# Pickup/drop locations along a track are represented by their horizontal projections.
DRAWING_POINTS = {"BOF_C": 551, "BOF_B": 631, "BOF_A": 708, "LF1": 945, "LF3": 253,
                  "RH1": 423, "RH2": 314, "CC1": 429, "CC2": 543, "CC3": 1029}
SOURCE_LABELS = {"A炉": "BOF_A", "B炉": "BOF_B", "C炉": "BOF_C", "1LF": "LF1", "3LF": "LF3",
                 "1RH": "RH1", "2RH": "RH2", "1号连铸": "CC1", "2号连铸": "CC2", "3号连铸": "CC3"}
# Prospective buffer positions are assumptions in the schematic coordinate system.
# The competition image does not associate the three permitted ferry IDs with rail coordinates.
ASSUMED_POINTS = {"SLAG": 762, "FERRY1": 551, "FERRY2": 631, "FERRY3": 708}
FERRIES = ("FERRY1", "FERRY2", "FERRY3")
CARS = (2, 4, 3, 5, 1)
LIMITS = (150, 450, 450, 450, 150)


# 作用：取经验分位数。输入：values 为样本，q 为概率。返回：按样本序位取得的分位点。
def pick_quantile(values, q):
    return sorted(values)[max(0, ceil(len(values) * q) - 1)]


# 作用：计算两个时间字符串间的时长。输入：a、b 分别为起止时间。返回：秒数。
def duration(a, b):
    return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())


# 作用：根据运输样本标定示意图时间尺度。输入：data 为已清洗数据。返回：尺度及标定诊断。
def calibration(data):
    samples = []
    for sample in data["transport_samples"]:
        left, right = (SOURCE_LABELS[sample[n]] for n in ("origin", "destination"))
        distance = abs(DRAWING_POINTS[left] - DRAWING_POINTS[right])
        seconds = duration(sample["pickup"], sample["arrival"])
        if seconds > 0:
            samples.append((sample["pickup"], left, right, distance, seconds))
    samples.sort()
    cutoff = samples[floor(len(samples) * .8)][0][:10]
    pairs = defaultdict(list)
    for day, a, b, length, seconds in samples:
        if day[:10] < cutoff and length >= 200:
            pairs[a, b].append(seconds / length)
    # Favor pairs whose transit makes the geometric component identifiable and
    # reduce the upward influence of unmeasured waiting or handling.
    estimates = {a + "->" + b: {"count": len(values), "q10_seconds_per_pixel": pick_quantile(values, .1)}
                 for (a, b), values in sorted(pairs.items()) if len(values) >= 15}
    scale = min(r["q10_seconds_per_pixel"] for r in estimates.values())
    holdout = [(distance, seconds) for date, _, _, distance, seconds in samples if date[:10] >= cutoff]
    under_observed = sum(round(distance * scale) > seconds for distance, seconds in holdout)
    return scale, {"method": "minimum pairwise empirical p10 on training pairs spanning >=200 pixels and >=15 samples",
                   "cutoff_holding_out_complete_final_dates": cutoff,
                   "training_n": len(samples) - len(holdout), "held_out_n": len(holdout),
                   "held_out_prediction_above_elapsed_n": under_observed, "pair_estimates": estimates}


# 作用：从实绩记录取区间分位数。输入：data 为历史数据，left、right 为字段名，q 为概率。返回：时长及样本统计。
def history_quantile(data, left, right, q):
    values = []
    for row in data["history"]:
        events = row["events"]
        if left in events and right in events:
            clock = duration(events[left], events[right])
            if clock > 0:
                values.append(clock)
    return {"seconds": pick_quantile(values, q) if values else None, "count": len(values), "q": q}


# 作用：估算各工位的同时占用数量。输入：data 为历史数据，cutoff 为训练截止时刻。返回：容量及样本诊断。
def buffer_capacity(data, cutoff):
    measurements = (
        ("lf", "in", "lf_arrive", "lf_process_start"),
        ("lf", "out", "lf_process_end", "lf_pickup"),
        ("rh", "in", "rh_arrive", "rh_process_start"),
        ("rh", "out", "rh_process_end", "rh_pickup"),
        ("caster", "in", "caster_ladle_seated", "casting_start"),
        ("caster", "empty", "casting_end", "empty_pickup"),
        ("converter", "out", "tapping_end", "loaded_pickup"),
    )
    groups = defaultdict(lambda: defaultdict(set))
    for row in data["history"]:
        for equipment, side, a, b in measurements:
            name, events = row[equipment], row["events"]
            if name and a in events and b in events and 0 < duration(events[a], events[b]) < 10800:
                groups[name + ":" + side][row["heat_id"]].add((events[a], events[b]))
    counts, diagnostics = {}, {}
    for resource, seen in groups.items():
        episodes = [(next(iter(pair)), heat) for heat, pair in seen.items() if len(pair) == 1]
        diagnostics[resource] = {}
        for kind, predicate in (("training", lambda s: s[:10] < cutoff),
                                ("holdout", lambda s: s[:10] >= cutoff)):
            active, peak, witness = set(), 0, None
            selected = [(a, b, heat) for (a, b), heat in episodes if predicate(a)]
            events = sorted((t, change, heat) for a, b, heat in selected for t, change in ((a, 1), (b, -1)))
            for t, change, heat in events:
                if change > 0:
                    active.add(heat)
                else:
                    active.remove(heat)
                if len(active) > peak:
                    peak, witness = len(active), {"timestamp": t, "heats": sorted(active)}
            diagnostics[resource][kind] = {"intervals": len(selected), "peak": peak, "example": witness}
            if kind == "training":
                counts[resource] = max(1, peak)
    return counts, diagnostics


# 作用：估计精炼结束到开浇的区间。输入：data 为历史数据，cutoff 为训练截止时刻。返回：各路线的样本和分位统计。
def casting_exposure(data, cutoff):
    """Estimate last-refining-finish to opening intervals for an optional planning cap."""
    history = {row["source_row"]: row for row in data["history"]}
    values = defaultdict(set)
    for sample in data["transport_samples"]:
        route = sample["route"]
        if not route.endswith("->CC"):
            continue
        events = history[sample["history_row"]]["events"]
        name = route[:2].lower() + "_process_end"
        if name in events and "casting_start" in events:
            seconds = duration(events[name], events["casting_start"])
            if 0 < seconds < 6 * 3600:
                values[route].add((sample["pickup"], sample["history_row"], seconds))
    result = {}
    for route, samples in values.items():
        training = [s for pickup, _, s in samples if pickup[:10] < cutoff]
        holdout = [s for pickup, _, s in samples if pickup[:10] >= cutoff]
        cap = pick_quantile(training, .95)
        result[route] = {"p95_seconds": cap, "training_n": len(training), "holdout_n": len(holdout),
                         "holdout_above_p95_n": sum(value > cap for value in holdout)}
    return result


# 作用：按明确假设构造行车调度输入。输入：data 为原料数据，production 为生产排程，scale_factor、clearance_pixels 为场地试算参数。返回：场地、重包、空包回收和供包任务。
def make_inputs(data, production, scale_factor=1., clearance_pixels=20):
    base, diagnostics = calibration(data)
    scale = base * scale_factor
    points = {key: round(pixel * scale) for key, pixel in (DRAWING_POINTS | ASSUMED_POINTS).items()}
    ferry_clear = 300  # Exclusive handoff reservation including exit from the E–F position.
    capacities, buffer_diagnostics = buffer_capacity(data, diagnostics["cutoff_holding_out_complete_final_dates"])
    epoch = min(datetime.fromisoformat(o["start"]) for o in production["operations"]) - timedelta(hours=2)
    stats = {"empty_pick_lag": history_quantile(data, "casting_end", "empty_pickup", .1),
             "slag_process": history_quantile(data, "slag_start", "slag_end", .5),
             "outbound_handoff_limit": history_quantile(data, "casting_end", "on_transfer_car", .95),
             "pre_bof_presence": history_quantile(data, "empty_on_converter_car", "tapping_start", .1),
             "outside_turnaround": history_quantile(data, "on_transfer_car", "hot_repair_out", .5)}
    site = {"origin": epoch.isoformat(sep=" "), "basis": "conditional schematic travel-time surrogate",
            "coordinate_unit": "seconds of uniform-speed horizontal travel",
            "calibration": diagnostics, "base_seconds_per_pixel": base, "seconds_per_pixel": scale,
            "casting_exposure": casting_exposure(data, diagnostics["cutoff_holding_out_complete_final_dates"]),
            "geometry_scale_factor": scale_factor, "schematic_points_pixels": DRAWING_POINTS,
            "assumed_points_pixels": ASSUMED_POINTS, "points": points,
            "bounds": [round(142 * scale), round(1326 * scale)],
            "initial": [round(x * scale) for x in (177, 708, 818, 1110, 1252)],
            "order": CARS, "limits": LIMITS, "clearance_pixels": clearance_pixels,
            "gaps": [max(1, round(clearance_pixels * scale))] * 4, "empirical_intervals": stats,
            "ferry_hold_seconds": ferry_clear, "return_pre_bof_window_seconds": 3600,
            "handling_switch_seconds": 0,
            "buffer_model": "effective simultaneous staged ladles from training records, separately by reservation type; "
                            "default one at unobserved buffers and each ferry",
            "buffer_capacities": capacities, "buffer_capacity_diagnostics": buffer_diagnostics,
            "unresolved_weight_policy": "eligible only for 450t cranes, conditional on package not exceeding 450t",
            "stock_policy": "exchangeable serviced ladles at transfer boundary and sufficient starting stock; "
                            "no same-ladle return-to-BOF binding",
            "unverified_cast_edges_policy": (
                "same caster and cast ID, adjacent formal heats in planned chronology with no explicit restart: contiguous"
                if production.get("network_adjustment", {}).get("continuous_by_cast_id")
                else "continuity imposed on sequence-confirmed edges only")}
    gross = available_weights(data, "gross_tonnes")
    tare = available_weights(data, "ladle_tare_tonnes")

    # 作用：统一转换实绩模式的时间。输入：value 为日期时刻。返回：相对场地基准的秒数。
    def seconds(value):
        return int((datetime.fromisoformat(value) - epoch).total_seconds())

    # 作用：按包重资格组装吊运任务。输入：name、row、heat 为任务标识，kind 为类型，origin、dests 为端点，ready、due 为窗口，weights 为包重，kwargs 为附加工位约束。返回：Job 任务对象。
    def job(name, row, heat, kind, origin, dests, ready, due, weights, **kwargs):
        raw = weights[heat]
        tonnes = next(iter(raw)) if len(raw) == 1 else None
        candidates = eligible_cranes(site, tonnes)
        return Job(name, row, heat, kind, origin, dests, ready, due, candidates, tonnes=tonnes, **kwargs)

    heavy = []
    for leg in production["handover_windows"]:
        row, heat = leg["source_row"], leg["heat_id"]
        origin, dest = leg["from_equipment"], leg["to_equipment"]
        route = leg["from_stage"] + "->" + leg["to_stage"]
        heavy.append(job(f"{row}:{route}", row, heat, "loaded", origin, (dest,),
                         seconds(leg["upstream_finished"]), seconds(leg["downstream_starts"]), gross,
                         source=origin + ":out", receiver=dest + ":in"))
    empty_pairs, feeds = [], []
    for op in production["operations"]:
        row, heat, equip = op["source_row"], op["heat_id"], op["equipment"]
        if op["stage"] == "CC":
            finish = seconds(op["end"])
            ready = finish + stats["empty_pick_lag"]["seconds"]
            due = finish + stats["outbound_handoff_limit"]["seconds"]
            out = job(f"{row}:CC->SLAG", row, heat, "empty_to_slag", equip, ("SLAG",), ready, due, tare,
                      source=equip + ":empty")
            ferry = job(f"{row}:SLAG->FERRY", row, heat, "empty_to_ferry", "SLAG", FERRIES, ready, due, tare,
                        before=stats["slag_process"]["seconds"], source="SLAG")
            empty_pairs.append((out, ferry))
        elif op["stage"] == "BOF":
            deadline = seconds(op["start"])
            window = site["return_pre_bof_window_seconds"]
            feeds.append(tuple(job(f"{row}:FERRY->BOF", row, heat, "empty_to_bof", f, (equip,),
                                   deadline - window, deadline, tare, source=None, receiver=equip + ":empty_in")
                               for f in FERRIES if points[f] != points[equip]))
    return site, heavy, empty_pairs, feeds
