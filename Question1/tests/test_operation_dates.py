import unittest

from model.production import operation_day_bounds


class OperationDatesTest(unittest.TestCase):
    # 作用：检验锁定开浇日后允许跨午夜结束。输入：self 为测试实例。返回：无。
    def test_lock_opening_date_allows_overnight_finishing(self):
        cast = {"kind": "CC", "target": 86390, "duration": 20}
        self.assertEqual(operation_day_bounds(cast, "production"), (0, 86399))
        self.assertEqual(operation_day_bounds(cast, "casting"), (86381, 86399))

    # 作用：检验午夜终点按半开区间归属前一天。输入：self 为测试实例。返回：无。
    def test_midnight_endpoint_occupies_the_earlier_day(self):
        cast = {"kind": "CC", "target": 86390, "duration": 10}
        self.assertEqual(operation_day_bounds(cast, "casting"), (0, 86390))
        self.assertEqual(operation_day_bounds({"kind": "LF", "target": 0, "duration": 1}, "production"), (0, None))


if __name__ == "__main__":
    unittest.main()
