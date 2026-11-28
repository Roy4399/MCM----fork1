"""第 5 节给定行程时间的验算，单位为分钟。"""
from statistics import fmean


# 作用：依次执行给定取送顺序，计算每包包等车、车等包、迟到。
# 参数：jobs 映射包名到 (就绪、起点、终点、期限)，order 为包名顺序，start 为车从坐标 0 出发的时刻。
# 返回：各包的 (待吊分钟，车等包分钟，迟到标志)。
def fixed_trip(jobs,order,start):
    t,x = start,0
    out={}
    for k in order:
        ready,src,dst,due=jobs[k]
        arrival=t+abs(src-x)
        pick=max(ready,arrival)
        t=pick+abs(dst-src)
        out[k]=(pick-ready,pick-arrival,int(t>due))
        x=dst
    return out


# 作用：用四个等权样本表示 0.25/0.50/0.25 分布并复算文档两表。
# 参数：无。
# 返回：各动作下每包的 (平均包等车，平均车等包，交付迟到率)。
def hand_cases():
    actions={'单包到8':(5,'A'),'单包到6':(3,'A'),
             '原地等5先A':(5,'AB'),'立即先A':(0,'AB'),'立即先B':(0,'BA')}
    result={}
    for label,(start,order) in actions.items():
        outcomes=[fixed_trip({'A':(r,3,0,11),'B':(0,0,-2,13)},order,start)
                  for r in (5,8,8,11)]
        result[label]={k:tuple(fmean(row[k][i] for row in outcomes) for i in range(3))
                       for k in order}
    return result
