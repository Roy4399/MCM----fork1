"""运行 python -m unittest discover -s tests -v。"""
import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path
from statistics import fmean
from model.dispatch.paired import choose
from model.dispatch.policy import Policy, candidates
from model.evaluation.audit import audit, metrics
from model.evaluation.compare import paired_differences
from model.evaluation.hand_cases import hand_cases
from model.forecast.duration import Forecast, quantile
from model.inputs.schema import Job, Plant, validate
from model.inputs.windows import load_case, DATA
from model.motion.track import Phase, Rail, evaluate, min_gap, segment, template
from model.replay.engine import apply, run, settle
from model.replay.state import Order, State


# 作用：创建五车、两条并行生产链的物理小窗口。
# 参数：无。两条链各有两个吊运任务，第二段受首段落包约束。
# 返回：(问题实例，确定性工时预测器，工时实现)。
def parallel_case():
    loc=dict(a=0,b=10,c=20,d=60,e=70,f=80)
    points=(-60,-40,0,10,20,60,70,80,110)
    rail=Rail(points,(3.,)*(len(points)-1),((3.,1.,1.),)*3,(-60,-40,110),5)
    jobs=(
        Job('a1','A','m','a','b',None,0,5,1,6,40,(2,)),
        Job('a2','A','n','b','c','a1',0,6,2,20,65,(2,),cast_time=10),
        Job('b1','B','m','d','e',None,0,5,1,6,40,(3,)),
        Job('b2','B','n','e','f','b1',0,6,2,20,65,(3,),cast_time=10))
    p=Plant(jobs,rail,(-60,-40,0,60,110),loc,dict.fromkeys(loc,1))
    validate(p)
    f=Forecast(jobs,[{'m':('*',1.),'n':('*',1.)}])
    return p,f,f.draw(9)


class TestModel(unittest.TestCase):
    # 作用：复核文中两张表的每行和提前两分钟的成本阈值。
    # 参数：self 为测试实例。
    # 返回：无；数值不符触发断言。
    def test_document_tables(self):
        out=hand_cases()
        self.assertEqual(out['单包到8']['A'],(.75,.75,.25))
        self.assertEqual(out['单包到6']['A'],(.25,2.25,.25))
        self.assertEqual((2.25-.75)/(.75-.25),3)
        for action,aw,bw,al,bl in [('原地等5先A',.75,11.75,.25,.25),
                                    ('立即先A',0.,11.,.25,.25),('立即先B',.5,0.,.25,0.)]:
            a,b=out[action]['A'],out[action]['B']
            self.assertEqual((a[0],b[0],a[2],b[2]),(aw,bw,al,bl))

    # 作用：按文档的速度、加速度和长度复核 11 秒，并拒绝不可达末速。
    # 参数：self 为测试实例。
    # 返回：无；数值或异常不符触发断言。
    def test_segment_example(self):
        acc,cruise,brake=segment(10,.4,.2,1,.5,.5)
        for actual,expected in zip((acc,cruise,brake,acc+cruise+brake),(1.2,8.2,1.6,11)):
            self.assertAlmostEqual(actual,expected)
        self.assertEqual(segment(.1,0,0,8,.5,.5)[1],0.)
        with self.assertRaises(ValueError):
            segment(.01,0,2.,3.,1.,1.)

    # 作用：检查正反向、带载档位、限速边界和边界速度连续性。
    # 参数：self 为测试实例。
    # 返回：无；运动轨迹违例触发断言。
    def test_gears_reverse_and_continuity(self):
        p,_,_=load_case('five-heats')
        for load in (0,1,2):
            for x,y in [(40,310),(310,40)]:
                tr=template(p.rail,x,y,load)
                xf,vf,_=evaluate(tr[-1],tr[-1].end)
                self.assertAlmostEqual(xf,y)
                self.assertAlmostEqual(vf,0)
                for a,b in zip(tr,tr[1:]):
                    xa,va,_=evaluate(a,a.end)
                    self.assertAlmostEqual(xa,b.x)
                    self.assertAlmostEqual(va,b.v)
        slow=replace(p.rail,edge_gears=(0,)*len(p.rail.limits))
        self.assertGreater(template(slow,40,310,2)[-1].end,template(p.rail,40,310,2)[-1].end)

    # 作用：构造两端均安全、段内相对速度为零处最危险的轨迹。
    # 参数：self 为测试实例。
    # 返回：无；漏检内部极值触发断言。
    def test_interior_gap(self):
        self.assertAlmostEqual(min_gap((0,()),(5,(Phase(0,2,5,-4,4),))),3)
        self.assertAlmostEqual(min_gap((0,()),(5,(Phase(0,2,5,-4,4),)),1.5),3.5)

    # 作用：检验归一均值、条件删失、整炉相关性与离散分位数。
    # 参数：self 为测试实例。
    # 返回：无；违反抽样口径触发断言。
    def test_conditioned_heat_blocks(self):
        p,_,_=parallel_case()
        f=Forecast(p.jobs,[{'m':('a',z),'n':('b',2*z)} for z in (.5,1,1.5)])
        self.assertEqual(fmean(r[0] for r in f.pool['A']),5)
        self.assertEqual(quantile([5,8,8,11],.25),5)
        self.assertEqual(f.belief('A',6,{'a1':0},{}),((7.5,9.),))
        self.assertGreater(f.belief('A',8,{'a1':0},{})[0][0],8)
        self.assertEqual(f.draw(11),f.draw(11))
        for row in f.pool['A']:
            self.assertAlmostEqual(row[0]/5,row[1]/6)

    # 作用：核验真正并发的两车四单、工艺递推、容量及五车间距。
    # 参数：self 为测试实例。
    # 返回：无；不同炉未并行或前序错误均失败。
    def test_two_simultaneous_cranes(self):
        p,f,w=parallel_case()
        s=run(State(p,f),w,Policy())
        self.assertGreaterEqual(audit(s),p.rail.gap)
        self.assertEqual(s.pick['a1'],s.pick['b1'])
        self.assertGreater(s.drop['a1'],s.pick['b1'])
        for k,parent in [('a2','a1'),('b2','b1')]:
            self.assertEqual(s.starts[k],s.drop[parent])
            self.assertEqual(s.ready[k],s.starts[k]+w[k]+p.jobs[[j.key for j in p.jobs].index(k)].prep)

    # 作用：确认一台车的原地待命不会冻结另一台车的派车机会。
    # 参数：self 为测试实例。
    # 返回：无；其他车辆被一起冻结时失败。
    def test_hold_is_local_to_one_crane(self):
        p,f,w=parallel_case()
        s=State(p,f)
        settle(s,w,Policy())
        apply(s,Order(2,'a1',wake=40),Policy())
        options,wake=candidates(s,Policy(),True)
        self.assertTrue(options)
        self.assertEqual({o.car for o in options},{3})
        self.assertLessEqual(wake,40)

    # 作用：断言同一观测历史面对不同隐藏未来得到相同候选和首步。
    # 参数：self 为测试实例。
    # 返回：无；若选择依赖未发生的完工时间则失败。
    def test_no_future_information(self):
        p,f,w=parallel_case()
        policy=Policy(scope='all')
        seen=[]
        for future in (w,{k:v*3 for k,v in w.items()}):
            s=State(p,f)
            settle(s,future,policy)
            choices,_=candidates(s,policy,True)
            seen.append((choices,choose(s,policy,choices)))
        self.assertEqual(seen[0],seen[1])

    # 作用：验证已有预约与窗首在制保留、预约结束同刻可接收。
    # 参数：self 为测试实例。
    # 返回：无；非法抢占或丢掉已加工时间触发断言。
    def test_external_reservation_and_wip(self):
        p,_,w=parallel_case()
        p=replace(p,jobs=(replace(p.jobs[0],initial_elapsed=2),*p.jobs[1:]),bookings=(('b',0.,20.),))
        f=Forecast(p.jobs,[{'m':('*',1.),'n':('*',1.)}])
        s=run(State(p,f),w,Policy())
        audit(s)
        self.assertEqual(s.ready['a1'],4)
        self.assertGreaterEqual(s.pick['a1'],20)
        self.assertLess(s.pick['b1'],20)

    # 作用：在 5 炉 12 段真实路线的四组工时上验证所有资格、接续及连续共轨约束。
    # 参数：self 为测试实例。
    # 返回：无；任何一组少单、错序或违约均失败。
    def test_five_heats_multiple_worlds(self):
        p,f,_=load_case('five-heats')
        self.assertEqual((len(f.groups),len(p.jobs),len(p.origins)),(5,12,5))
        for seed in (3,7,11,27):
            for policy in (Policy(timing='plan'),Policy(timing='fixed'),Policy(),Policy(estimate='average')):
                s=run(State(p,f),f.draw(seed),policy)
                m=metrics(s,p.jobs[0].key)
                self.assertEqual((m['tasks'],m['violations']),(12,0))
                self.assertGreaterEqual(m['min_gap']+1e-8,p.rail.gap)

    # 作用：让两个配对回放策略各自完成同一批 12 段任务，并核对首步排序已输出。
    # 参数：self 为测试实例。
    # 返回：无；共轨、缺单、首步选择或排序输出异常时失败。
    def test_paired_replay_twelve_jobs(self):
        p,f,_=load_case('five-heats')
        world=f.draw(27)
        for scope in ('target','all'):
            s=run(State(p,f),world,Policy(scope=scope,draws=2),choose)
            audit(s)
            self.assertEqual(len(s.drop),12)
            self.assertTrue(any(len(row['candidates'])>=2 for row in s.rankings))
            for row in s.rankings:
                self.assertEqual(row['draws'],2)
                self.assertTrue(all(item['failures']>=0 for item in row['candidates']))

    # 作用：测试共享 RH 与连铸排序反锁回归，核对模型断浇与现场实绩区分。
    # 参数：self 为测试实例。
    # 返回：无；任务未完成、虚报现场断浇或样本串用则失败。
    def test_holdout_lock_and_split(self):
        raw=json.loads((DATA/'plant_windows.json').read_text())
        self.assertLess(max(r['date'] for r in raw['training']),min(raw['dates']))
        p,f,w=load_case('2026-10-09')
        s=run(State(p,f),w,Policy())
        m=metrics(s,p.jobs[0].key)
        self.assertEqual(m['tasks'],len(p.jobs))
        self.assertIsNone(m['observed_breaks'])
        self.assertGreater(m['late_count'],0)

    # 作用：核验独立审计能拒绝错车、缺单和出窗首项承诺违约。
    # 参数：self 为测试实例。
    # 返回：无；若审计放行违规解则断言失败。
    def test_audit_rejects_invalid_results(self):
        p,f,w=parallel_case()
        s=run(State(p,f),w,Policy())
        bad=s.fork()
        bad.owner['a1']=0
        with self.assertRaises(ValueError):
            audit(bad)
        bad=s.fork()
        del bad.drop['a1']
        with self.assertRaises(ValueError):
            audit(bad)
        p=replace(p,jobs=(*p.jobs[:-1],replace(p.jobs[-1],commitment=1)))
        with self.assertRaisesRegex(ValueError,'交付承诺'):
            audit(run(State(p,f),w,Policy()))

    # 作用：验证重包承载资格及空包回程只能落到指定渡车。
    # 参数：self 为测试实例。
    # 返回：无；错误指派未拒绝或合法回程不能完成则失败。
    def test_load_class_and_ferry_return(self):
        p,f,_=load_case('five-heats')
        first=replace(p.jobs[0],eligible=(0,))
        with self.assertRaisesRegex(ValueError,'重包'):
            validate(replace(p,jobs=(first,)))
        empty=replace(first,load=1,ferry_return=True)
        with self.assertRaisesRegex(ValueError,'渡车'):
            validate(replace(p,jobs=(empty,)))
        empty=replace(empty,dest='1#渡车',eligible=(2,3,4),cast_time=0.)
        p=replace(p,jobs=(empty,))
        validate(p)
        f=Forecast(p.jobs,[{'BOF':('*',1.)}])
        state=run(State(p,f),f.draw(1),Policy())
        audit(state)
        self.assertEqual(len(state.drop),1)

    # 作用：验证去邻位待命后仍须支付最后一段行程，计入接包到达时刻。
    # 参数：self 为测试实例。
    # 返回：无；提前到达或遗漏待命位至取包点的移动则失败。
    def test_standby_last_leg(self):
        p,f,w=parallel_case()
        p=replace(p,jobs=(p.jobs[0],),rail=replace(p.rail,parking=(10,)))
        s=State(p,f)
        settle(s,w,Policy())
        apply(s,Order(2,'a1',10),Policy())
        s=run(s,{'a1':w['a1']},Policy())
        audit(s)
        kinds=[(label,b-a) for _,_,label,a,b in s.logs]
        self.assertEqual([l for l,_ in kinds],['standby','empty','loaded'])
        self.assertGreaterEqual(s.arrival['a1'],kinds[0][1]+kinds[1][1]-1e-8)

    # 作用：确认配对区间按日期而非场景重复数加权。
    # 参数：self 为测试实例。
    # 返回：无；分组计算或单日区间有误触发断言。
    def test_paired_date_bootstrap(self):
        rows=[]
        for date,w,diff in [('d1','s1',2),('d1','s2',2),('d2','s1',6)]:
            for label,v in [('a',0),('b',diff)]:
                from collections import defaultdict
                rows.append(dict(case=date,world=w,policy=label,status='ok',
                                 metrics=defaultdict(float,wait_mean=v)))
        metric=paired_differences(rows,['a','b'],2,100)['b']['wait_mean']
        self.assertEqual((metric['mean'],metric['blocks'],metric['pairs']),(4,2,3))
        self.assertEqual(metric['ci95'],[2.,6.])

    # 作用：检查每个显式函数在定义（或装饰器）前具有作用、参数含义及返回值说明。
    # 参数：self 为测试实例。
    # 返回：无；漏注释时指出文件和函数。
    def test_comments_on_every_function(self):
        root=Path(__file__).resolve().parents[1]
        for folder in ('model','runner','tests'):
            for file in (root/folder).rglob('*.py'):
                src=file.read_text(encoding='utf8')
                lines=src.splitlines()
                for node in ast.walk(ast.parse(src)):
                    if isinstance(node,ast.FunctionDef):
                        first=min([node.lineno]+[d.lineno for d in node.decorator_list])
                        comment='\n'.join(lines[max(0,first-4):first-1])
                        for word in ('作用','参数','返回'):
                            self.assertIn(word,comment,f'{file}/{node.name}')


if __name__=='__main__':
    unittest.main()
