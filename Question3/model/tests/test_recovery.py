import unittest
from dataclasses import replace
from model.experiments.cases import repair_case,rail_case
from model.motion.rail import safe
from model.recovery.solver import solve
from model.recovery.rolling import recover
from model.recovery.decision import common_dispatch
from model.domain.types import Crane,Trip


class RecoveryTests(unittest.TestCase):
    # 作用：核对手算两维修情景的全部迟到与等待。参数：self 测试实例。返回：无，差异触发断言。
    def test_hand_table(self):
        for repair,late,wait,fixed in [(8,18,20,4),(15,25,27,2)]:
            case=repair_case(repair)
            for policy,expected in [('original',(late,wait)),('fill',(0,fixed)),('cps',(0,fixed))]:
                result=solve(case,policy,{'A','B'})
                self.assertEqual(result['score'][:2],expected)
                self.assertEqual(result['trips'][-1].start,35)

    # 作用：检查不能完整回填时的换序收益。参数：self 测试实例。返回：无，差异触发断言。
    def test_partial_slot(self):
        case=repair_case(short_slot=True)
        self.assertEqual(solve(case,'fill',{'A','B'})['score'][0],13)
        self.assertEqual(solve(case,'cps',{'A','B'})['score'][0],0)

    # 作用：核验工艺边及禁止换序。参数：self 测试实例。返回：无，违例触发断言。
    def test_precedence(self):
        case=repair_case(dependent=True)
        for policy in ('fill','cps'):
            self.assertEqual(solve(case,policy,{'A','B'})['trips'][0].task,'A')

    # 作用：检查两个未来情景共享首步。参数：self 测试实例。返回：无，错误触发断言。
    def test_common_action(self):
        self.assertEqual(common_dispatch([repair_case(),repair_case(15)],{'A','B'})[:2],('B',0))

    # 作用：校验三种窗口均完成五车多任务且保持间距。参数：self 测试实例。返回：无，错误触发断言。
    def test_five_cranes(self):
        case=rail_case()
        for mode in ['fixed','graph','no_neighbor']:
            r=recover(case,{'A':4},mode)
            self.assertEqual(len(r['trips']),5)
            self.assertTrue(safe(case.cranes,r['trips'],0,case.horizon,case.gap))

    # 作用：检验端点安全但中途间距不足的抛物线。参数：self 测试实例。返回：无，漏检触发断言。
    def test_interior_conflict(self):
        c=(Crane('L',0,450),Crane('R',2,450))
        tr=Trip('R','R',0,2,((0,2,2,-3,3),),0)
        self.assertFalse(safe(c,(tr,),0,2,1))

    # 作用：检查设备停机区间与车载重。参数：self 测试实例。返回：无，错误触发断言。
    def test_outage_capacity(self):
        base=repair_case()
        t=replace(base.tasks[1],station='LF',dwell=4)
        case=replace(base,tasks=(t,),outages=(('LF',10,14),))
        self.assertEqual(solve(case,'cps',{'B'})['trips'][0].end,14)
        case=replace(case,tasks=(replace(t,weight=500),))
        self.assertIsNone(solve(case,'cps',{'B'})['score'])

    # 作用：区分缺包、停机及缺少实绩。参数：self 测试实例。返回：无，错误触发断言。
    def test_casting_metrics(self):
        from model.experiments.metrics import casting_gaps
        self.assertIsNone(casting_gaps(None))
        self.assertEqual(casting_gaps([(10,14,1,'缺包'),(20,26,1,'停机')]),{'缺包':3,'停机':5})

    # 作用：冻结边界不可行时验证自动扩窗。参数：self 测试实例。返回：无，错误触发断言。
    def test_expand_window(self):
        case=repair_case(15)
        r=recover(case,{'A':15},'fixed',width=1)
        # B 可保持 10:20 开始；加入禁止前置后，固定 B 将造成边界不可行。
        case=repair_case(15,dependent=True)
        r=recover(case,{'A':15},'fixed',width=1)
        self.assertEqual(r['expansions'],1)
        self.assertIsNotNone(r['score'])
