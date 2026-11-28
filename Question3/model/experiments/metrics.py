# 作用：计算已核实连续浇次的实际缺口。参数：events 为(前炉终浇,后炉开浇,允许交接分钟,原因)记录，None 表示无实绩。返回：按原因累计的分钟数字典或 None。
def casting_gaps(events):
    if events is None:
        return None
    totals={}
    for previous,next_start,allowed,cause in events:
        if allowed<0:
            raise ValueError('允许交接间隔不能小于零')
        totals[cause]=totals.get(cause,0)+max(0,next_start-previous-allowed)
    return totals
