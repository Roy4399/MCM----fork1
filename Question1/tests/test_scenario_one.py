import unittest

from model.production import route_buffers
from model.site import build_problem
from runner.scenario_one import compare, relax_transport


# 作用：构造一炉钢和五台行车的合成场景。输入：无。返回：生产排程、炉次输入和场地。
def site_example():
    # 作用：生成合成场景的时刻。输入：second 为分钟内秒数。返回：日期时间字符串。
    def at(second):
        return f"2026-01-01 00:00:{second:02d}"

    ops = []
    for kind, equipment, start, end in (("BOF", "BOF_A", 0, 1), ("LF", "LF1", 3, 5), ("CC", "CC1", 7, 9)):
        ops.append({"source_row": 2, "heat_id": "synthetic", "stage": kind, "equipment": equipment,
                    "start": at(start), "end": at(end), "duration_seconds": end - start})
    legs = []
    for left, right in zip(ops, ops[1:]):
        legs.append({"source_row": 2, "heat_id": "synthetic", "from_stage": left["stage"],
                     "to_stage": right["stage"], "from_equipment": left["equipment"],
                     "to_equipment": right["equipment"], "upstream_finished": left["end"],
                     "downstream_starts": right["start"]})
    production = {"operations": ops, "handover_windows": legs, "scope": {"unconfirmed_cast_edges": []}}
    data = {"history": [{"heat_id": "synthetic", "gross_tonnes": 410.0, "ladle_tare_tonnes": 120.0}],
            "jobs": [{"source_row": 2, "cast_id": "T", "cast_sequence": 1,
                      "replace_tundish": False, "rethread": False}]}

    # 作用：构造工位运输规则。输入：src、dst 为起落坐标，source、receiver 为工位名。返回：运输规则字典。
    def route(src, dst, source, receiver):
        return {"src": src, "dst": dst, "source": source, "receiver": receiver,
                "eligible_cranes": [1, 3], "alpha_seconds": 0, "beta_seconds": 0, "release_lag_seconds": 0}

    coords = [(3, 0), (1, 6), (2, 10), (4, 12), (5, 14)]
    site = {
        "origin": at(0), "tick_seconds": 1, "grid_spacing_metres": 1, "horizon_ticks": 12,
        "cranes": [{"id": c, "initial": x, "edges": [[i, i + 1] for i in range(3)] if c == 3 else []}
                   for c, x in coords],
        "safety_gaps": [2, 2, 2, 2], "parking": [*range(4), 6, 10, 12, 14],
        "capacities": {name: 1 for name in ("BOF", "LF", "CC", "O1", "R1", "O2", "R2", "O3", "R3")},
        "processing_resources": {"BOF_A": "BOF", "LF1": "LF", "CC1": "CC"},
        "bookings": [], "cast_edge_decisions": {}, "caster_changeover_seconds_by_row": {},
        "transfer_rules": {"BOF_A->LF1": route(0, 1, "O1", "R1"), "LF1->CC1": route(1, 2, "O2", "R2")},
        "ferry_positions": {"1": 3},
        "empty_returns": [{"source_row": 2, "upstream": at(9), "downstream": at(11), "return_ferry": 1,
                           **route(2, 3, "O3", "R3")}],
    }
    return production, data, site


class ScenarioOneTests(unittest.TestCase):
    # 作用：检验生产计划可转换为含空包的五车任务。输入：self 为测试实例。返回：无。
    def test_production_to_five_cranes_includes_empty_ladle(self):
        production, data, site = site_example()
        p = build_problem(production, data, site)
        self.assertEqual(["2:BOF->LF", "2:LF->CC", "2:empty:1"], [task.name for task in p.tasks])
        self.assertEqual((0,), p.tasks[0].cars)
        self.assertEqual(1, p.tasks[-1].return_ferry)
        self.assertIn(("LF", 3, 5), p.bookings)
        result = compare(p, 10)
        self.assertEqual("AUDITED_CRANE_PLAN", result["status"])
        self.assertEqual(0, result["strategies"]["joint"]["metrics_ticks"]["wait"])
        self.assertEqual(3, result["strategies"]["joint"]["metrics_ticks"]["loaded"])

    # 作用：检验必要场地输入与重包运输窗口。输入：self 为测试实例。返回：无。
    def test_checks_required_inputs_and_transport_window(self):
        production, data, site = site_example()
        site["empty_returns"][0]["downstream"] = site["empty_returns"][0]["upstream"]
        p = build_problem(production, data, site)
        self.assertEqual("short_window", relax_transport(p)[0]["reason"])
        site["ferry_positions"]["1"] = 2
        with self.assertRaisesRegex(ValueError, "渡车坐标"):
            build_problem(production, data, site)

    # 作用：检验经验运输时间仅计入有效正时长。输入：self 为测试实例。返回：无。
    def test_empirical_transport_uses_only_positive_measured_travel(self):
        data = {"transport_samples": [
            {"route": "BOF->LF", "pickup": "2026-01-01 00:00:10", "arrival": f"2026-01-01 00:00:{end}"}
            for end in ("05", "11", "14")
        ]}
        self.assertEqual({"BOF->LF": {"seconds": 3, "sample_count": 2}}, route_buffers(data))
