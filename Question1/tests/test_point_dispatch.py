import unittest

from model.local_dispatch import solve_insertions
from model.point_dispatch import Job, Lift, Reservations, verify


class PointDispatchTests(unittest.TestCase):
    # 作用：设置测试用的短轨道和车辆。输入：self 为测试实例。返回：无。
    def setUp(self):
        self.site = {"bounds": [0, 12], "initial": [0, 11], "gaps": [2],
                     "points": {"W": 2, "E": 8}, "limits": [450, 450],
                     "buffer_capacities": {"source": 2}}

    # 作用：检验共轨相向任务不能越车。输入：self 为测试实例。返回：无。
    def test_opposite_lifts_fail_without_passing(self):
        a = Job("a", 2, "heat-a", "loaded", "W", ("E",), 3, 9, (0,))
        b = Job("b", 3, "heat-b", "loaded", "E", ("W",), 3, 9, (1,))
        state = Reservations(self.site)
        self.assertIsNone(solve_insertions(state, [a, b], [], 1.))
        chosen = next(state.proposals(a))
        state.add(a, chosen)
        result = verify(self.site, [a], state.lifts, state.trace(), state.resources)
        self.assertEqual(result["loaded_motion_seconds"], 6)
        self.assertGreaterEqual(result["min_adjacent_gaps"][0], 2)

    # 作用：检验缓存释放时刻可立即交接。输入：self 为测试实例。返回：无。
    def test_saturation_respects_half_open_handover(self):
        state = Reservations(self.site)
        state.reserve("source", 2, 7, "a")
        state.reserve("source", 4, 8, "b")
        state.reserve("source", 7, 9, "c")
        self.assertEqual([(a, b) for a, b, _ in state.saturated("source")], [(4, 7), (7, 8)])

    # 作用：检验回放拒绝用 150 吨车运输未知包重。输入：self 为测试实例。返回：无。
    def test_verify_rejects_unknown_weight_on_150_tonne_crane(self):
        job = Job("move", 2, "heat", "loaded", "W", ("E",), 2, 8, (0,))
        state = Reservations(self.site)
        state.add(job, next(state.proposals(job)))
        traces = state.trace()
        self.site["limits"][0] = 150
        with self.assertRaisesRegex(ValueError, "load qualification"):
            verify(self.site, [job], state.lifts, traces, state.resources)

    # 作用：检验原位交接占用车辆且无横移。输入：self 为测试实例。返回：无。
    def test_colocated_transfer_uses_car_but_not_horizontal_motion(self):
        self.site["points"]["FERRY"] = 2
        self.site["colocated_ferry_transfer_seconds"] = 4
        job = Job("transfer", 2, "heat", "empty_to_bof", "FERRY", ("W",), 2, 10, (0,))
        second = Job("other", 3, "other", "loaded", "W", ("E",), 4, 10, (0,))
        state = Reservations(self.site)
        lift = next(state.proposals(job))
        self.assertEqual((2, 6), (lift.pickup, lift.arrival))
        state.add(job, lift)
        self.assertFalse(list(state.proposals(second, latest=4)))
        result = verify(self.site, [job], state.lifts, state.trace(), state.resources)
        self.assertEqual((0, 4, 2), (result["loaded_motion_seconds"],
                                    result["stationary_service_seconds"], result["empty_motion_seconds"]))
        invalid_trace = (((0, 0), (2, 2), (3, 3), (4, 2)), ((0, 11),))
        with self.assertRaisesRegex(ValueError, "moved during stationary transfer"):
            verify(self.site, [job], state.lifts, invalid_trace, state.resources)

    # 作用：检验原位交接全程与邻车保持间距。输入：self 为测试实例。返回：无。
    def test_colocated_proposal_checks_neighbour_inside_stationary_interval(self):
        self.site["points"].update(FERRY=5, BOF=5)
        self.site["colocated_ferry_transfer_seconds"] = 12
        job = Job("transfer", 2, "heat", "empty_to_bof", "FERRY", ("BOF",), 5, 30, (0,))
        state = Reservations(self.site)
        state.anchors[1].append((11, 6))
        self.assertFalse(list(state.proposals(job, latest=5)))
        self.assertEqual(12, next(state.proposals(job)).pickup)


if __name__ == "__main__":
    unittest.main()
