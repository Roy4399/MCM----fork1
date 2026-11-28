"""Convert a fixed production candidate plus explicit site inputs into a Problem."""

from collections import defaultdict
from datetime import datetime
from math import isfinite

from model.map import Track
from model.problem import Problem, validate
from model.windows import Cast, Task, cast_bookings


CAPACITY_TONNES = {1: 150, 2: 150, 3: 450, 4: 450, 5: 450}


# 作用：给重包交接生成工艺路线名。输入：leg 为工序交接记录。返回：来源和去向组成的路线名。
def route_name(leg):
    return f'{leg["from_equipment"]}->{leg["to_equipment"]}'


# 作用：列出生产计划之外还需提供的现场量。输入：production 为生产计划，data 为清洗后的附件。返回：所需物理输入和说明。
def required_inputs(production, data):
    """Report which physical inputs remain external to the production sheets."""
    observations = available_weights(data, "gross_tonnes")
    empty_weights = available_weights(data, "ladle_tare_tonnes")
    heats = sorted({op["heat_id"] for op in production["operations"]})
    return {
        "dispatch_geometry_in_source": data["resources"].get("dispatch_geometry") is not None,
        "required_site_fields": [
            "origin", "tick_seconds", "grid_spacing_metres", "horizon_ticks", "cranes",
            "safety_gaps", "parking", "capacities", "processing_resources", "transfer_rules", "empty_returns",
            "ferry_positions", "bookings", "cast_edge_decisions", "caster_changeover_seconds_by_row",
        ],
        "transfer_rules_for_equipment_pairs": sorted({route_name(x) for x in production["handover_windows"]}),
        "heats_without_unambiguous_gross_weight": [h for h in heats if len(observations[h]) != 1],
        "heats_without_unambiguous_tare_weight": [h for h in heats if len(empty_weights[h]) != 1],
        "empty_return_scope_to_confirm": len(heats),
        "unconfirmed_cast_edges": production["scope"]["unconfirmed_cast_edges"],
    }


# 作用：收集炉次的可用重量。输入：data 为清洗记录，field 为重量字段名。返回：炉号到可用重量集合的映射。
def available_weights(data, field):
    weights = defaultdict(set)
    for row in data["history"]:
        value = row.get(field)
        if isinstance(value, (int, float)) and isfinite(value) and value > 0:
            weights[row["heat_id"]].add(value)
    return weights


# 作用：把生产计划转换为离散时空模型。输入：production 为生产计划，data 为炉次和重量数据，site 为现场参数。返回：经检查的 Problem 对象。
def build_problem(production, data, site):
    """Require real or explicitly supplied geometry and task times at the crane boundary."""
    epoch = datetime.fromisoformat(site["origin"])
    tick_seconds = site["tick_seconds"]
    spacing = site["grid_spacing_metres"]
    if type(tick_seconds) is not int or tick_seconds <= 0:
        raise ValueError("tick_seconds 必须是正整数")
    if not isinstance(spacing, (int, float)) or not isfinite(spacing) or spacing <= 0:
        raise ValueError("grid_spacing_metres 必须为正")

    # 作用：把时长换算成离散拍数。输入：seconds 为时长秒数。返回：整数拍数。
    def ticks(seconds):
        if type(seconds) is not int or seconds < 0 or seconds % tick_seconds:
            raise ValueError(f"原始时间 {seconds} 秒无法精确落入网格，须调整时间精度")
        return seconds // tick_seconds

    # 作用：把日期时刻换算为轨道时间。输入：text 为日期时刻字符串。返回：相对基准的拍数。
    def time_tick(text):
        elapsed = (datetime.fromisoformat(text) - epoch).total_seconds()
        if elapsed != int(elapsed):
            raise ValueError("当前模型要求输入达到整秒精度")
        return ticks(int(elapsed))

    cranes = site["cranes"]
    crane_ids = [c["id"] for c in cranes]
    if sorted(crane_ids) != sorted(CAPACITY_TONNES):
        raise ValueError("必须提供从左到右排列的 1–5 号行车")
    index_of = {crane_id: index for index, crane_id in enumerate(crane_ids)}
    track = Track(tuple(c["initial"] for c in cranes),
                  tuple(frozenset(tuple(e) for e in c["edges"]) for c in cranes),
                  frozenset(site["parking"]), tuple(site["safety_gaps"]))
    capacities = dict(site["capacities"])
    bookings = [(b["resource"], time_tick(b["start"]), time_tick(b["end"])) for b in site["bookings"]]
    weights = {field: available_weights(data, field) for field in ("gross_tonnes", "ladle_tare_tonnes")}

    # 作用：提取某炉的唯一可用重量。输入：heat 为炉号，field 为重量字段。返回：吨数；无法唯一确定时为 None。
    def load(heat, field):
        override = site.get(field + "_by_heat", {}).get(heat)
        options = weights[field][heat]
        value = override if override is not None else next(iter(options)) if len(options) == 1 else None
        if not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
            raise ValueError(f"{heat} 缺少经确认的 {field}")
        return value

    # 作用：按工位规则生成一个调度任务。输入：name 为任务名，rule 为路线规则，upstream、downstream 为工序时刻，tonnes 为包重，parents 为前置任务，ferry 为渡车编号。返回：Task 任务对象。
    def task(name, rule, upstream, downstream, tonnes, parents=(), ferry=None):
        candidates = tuple(
            index_of[k] for k in rule["eligible_cranes"] if tonnes <= CAPACITY_TONNES[k])
        deadline = rule.get("latest_pick_at")
        return Task(
            name, rule["src"], rule["dst"], time_tick(upstream), time_tick(downstream), candidates,
            alpha=ticks(rule["alpha_seconds"]), beta=ticks(rule["beta_seconds"]),
            latest_pick=time_tick(deadline) if deadline else None, parents=parents,
            source=rule["source"], receiver=rule["receiver"],
            release_lag=ticks(rule["release_lag_seconds"]), return_ferry=ferry,
        )

    tasks = []
    to_cast = {}
    operations = {(o["source_row"], o["stage"]): o for o in production["operations"]}
    equipment = {o["equipment"] for o in operations.values()}
    processing = site["processing_resources"]
    if not equipment <= processing.keys() or len({processing[m] for m in equipment}) != len(equipment):
        raise ValueError("每台生产设备须对应不同且明确的处理位资源")
    bookings.extend((processing[o["equipment"]], time_tick(o["start"]), time_tick(o["end"]))
                    for o in operations.values() if o["stage"] != "CC")
    last_for_heat = {}
    for leg in production["handover_windows"]:
        row = leg["source_row"]
        from_stage, to_stage = leg["from_stage"], leg["to_stage"]
        name = f"{row}:{from_stage}->{to_stage}"
        rule = {**site["transfer_rules"][route_name(leg)], **site.get("transfer_overrides", {}).get(name, {})}
        before = last_for_heat.get(row)
        parents = ((before, ticks(operations[row, from_stage]["duration_seconds"])),) if before else ()
        t = task(name, rule, leg["upstream_finished"], leg["downstream_starts"],
                 load(leg["heat_id"], "gross_tonnes"), parents)
        tasks.append(t)
        last_for_heat[row] = name
        if to_stage == "CC":
            to_cast[row] = name

    # An explicit break decision is needed wherever the input plan cannot identify
    # a continuous transition from chronology and the sequence numbers together.
    decisions = site["cast_edge_decisions"]
    unconfirmed = {f'{p["left_row"]}:{p["right_row"]}' for p in production["scope"]["unconfirmed_cast_edges"]}
    if decisions.keys() - unconfirmed:
        raise ValueError("确认记录只能用于生产候选中的待核连浇接续")
    missing = sorted(key for key in unconfirmed if decisions.get(key) not in ("continuous", "break"))
    if missing:
        raise ValueError(f"需给出 {len(missing)} 处未确认连浇接续的依据，首项 {missing[0]}")
    jobs = {j["source_row"]: j for j in data["jobs"]}
    casters = defaultdict(list)
    for (row, stage), op in operations.items():
        if stage == "CC":
            casters[op["equipment"]].append(op)
    casts = []
    changeovers = site["caster_changeover_seconds_by_row"]
    for machine, entries in casters.items():
        previous = None
        group_number = 0
        for op in sorted(entries, key=lambda item: item["start"]):
            row = op["source_row"]
            job = jobs[row]
            if previous is None or job["cast_id"] != previous["cast_id"] or job["replace_tundish"] or job["rethread"]:
                continuous = False
            else:
                disposition = decisions.get(f'{previous["source_row"]}:{row}')
                continuous = disposition == "continuous" if disposition else job["cast_sequence"] == previous["cast_sequence"] + 1
            group_number += not continuous
            if (job["replace_tundish"] or job["rethread"]) and str(row) not in changeovers:
                raise ValueError(f"原表第 {row} 行有换中包或重引锭标记，需给出确认的切换占用时间")
            change = ticks(changeovers.get(str(row), 0))
            casts.append(Cast(processing[machine], str(group_number), to_cast[row],
                              time_tick(op["start"]), time_tick(op["end"]), change))
            previous = job
    bookings.extend(cast_bookings(casts, tasks))

    returns = defaultdict(list)
    ferry_positions = site["ferry_positions"]
    for record in site["empty_returns"]:
        returns[record["source_row"]].append(record)
    if returns.keys() != jobs.keys():
        raise ValueError("全量计划要求逐炉给出空包回程行车任务")
    for row, records in returns.items():
        cast = operations[row, "CC"]
        heat = cast["heat_id"]
        if records[-1].get("return_ferry") not in (1, 2, 3):
            raise ValueError(f"{heat} 末段空包回程必须落到 1–3 号渡车")
        ferry = records[-1]["return_ferry"]
        if records[-1]["dst"] != ferry_positions[str(ferry)]:
            raise ValueError(f"{heat} 空包落点与 {ferry} 号渡车坐标不一致")
        parent = to_cast[row]
        for index, record in enumerate(records, 1):
            if datetime.fromisoformat(record["upstream"]) < datetime.fromisoformat(cast["end"]):
                raise ValueError(f"{heat} 空包任务早于终浇")
            lag = ticks(cast["duration_seconds"] if index == 1 else record["preceding_handover_seconds"])
            name = f"{row}:empty:{index}"
            tasks.append(task(name, record, record["upstream"], record["downstream"],
                              load(heat, "ladle_tare_tonnes"), ((parent, lag),), record.get("return_ferry")))
            parent = name
    p = Problem(tuple(tasks), track, site["horizon_ticks"], capacities, tuple(bookings))
    validate(p)
    return p
