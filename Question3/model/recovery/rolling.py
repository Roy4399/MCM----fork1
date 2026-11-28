from dataclasses import replace
from model.propagation.window import affected
from model.recovery.solver import solve


# 作用：处理已观测到的就绪更新，保护窗口外预约，必要时扩窗。参数：case 决策快照，updates 为任务名至最新可吊时刻，mode 指定 fixed/graph/no_neighbor，width 固定窗长度。返回：派工结果及窗口统计。
def recover(case,updates,mode='graph',width=10):
    if mode not in ('fixed','graph','no_neighbor'):
        raise ValueError(mode)
    if not set(updates)<={t.name for t in case.tasks}:
        raise ValueError('未知任务')
    current=replace(case,tasks=tuple(replace(t,ready=updates.get(t.name,t.ready)) for t in case.tasks))
    active=({t.name for t in current.tasks if t.original<case.now+width} if mode=='fixed'
            else affected(current,neighbors=mode=='graph'))
    active.update(updates)
    active -= {t.name for t in current.tasks if t.locked}
    count=len(active)
    result=solve(current,'cps',active)
    expansions=0
    if result['score'] is None:
        expanded={t.name for t in current.tasks if not t.locked}
        if expanded!=active:
            expansions=1
            active=expanded
            result=solve(current,'cps',active)
    return dict(result,initial_count=count,final_count=len(active),expansions=expansions)
