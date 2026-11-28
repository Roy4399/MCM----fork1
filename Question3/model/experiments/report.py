from time import perf_counter
from model.recovery.rolling import recover
from model.experiments.cases import rail_case


# 作用：生成可读的实验记录。参数：result 求解结果。返回：可 JSON 序列化的记录，时刻单位分钟。
def record(result):
    return dict(result,trips=[(t.task,t.crane,t.start,t.end) for t in result['trips']])


# 作用：用完全相同的五车实例比较窗口策略。参数：modes 窗口策略序列。返回：每种策略的指标与排程。
def window_comparison(modes):
    case=rail_case()
    rows={}
    for mode in modes:
        start=perf_counter()
        rows[mode]=record(recover(case,{'A':4},mode))
        rows[mode]['seconds']=round(perf_counter()-start,4)
    return rows
