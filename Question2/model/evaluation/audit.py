"""从完整轨迹和起落记录复算硬约束及绩效，不使用候选的自评分。"""
from collections import defaultdict
from math import inf
from statistics import fmean
from model.forecast.duration import quantile
from model.motion.track import EPS, min_gap, state_at, template


# 作用：独立审计任务完整性、车位、资格、容量、接续及连续车距。
# 参数：s 为待验收的完整回放终态。
# 返回：各相邻对中最小的安全间距（单车返回无穷）；不合格抛出 ValueError。
def audit(s):
    if s.blocked or set(s.drop)!=set(s.jobs):
        raise ValueError(s.blocked or "有任务未完成")
    slots = defaultdict(list)
    intervals = defaultdict(list)
    for name,a,b in s.plant.bookings:
        slots[name].append((a,b))
    for j in s.plant.jobs:
        k = j.key
        c = s.owner[k]
        a,b = s.pick[k],s.drop[k]
        if (c not in j.eligible or a+EPS<max(s.ready[k],s.arrival[k]) or b+EPS<a
                or b>j.commitment+EPS or s.ready[k]+EPS<s.ends[k]+j.prep):
            raise ValueError(f"{k} 资格、就绪或交付承诺不符")
        if j.prev and s.starts[k]+EPS<s.drop[j.prev]:
            raise ValueError(f"{k} 违反前序")
        if not j.prev:
            if s.starts[k]+EPS<j.release and not j.initial_elapsed:
                raise ValueError(f"{k} 提前开工")
            slots[j.source].append((s.starts[k],a))
        next_job = s.child.get(k)
        end = s.pick[next_job.key] if next_job else s.casts[k]
        slots[j.dest].append((a,end))  # 在起吊时预留接收位
        intervals[c].append((a,b))
        for t,station in ((a,j.source),(b,j.dest)):
            x,v,_ = state_at(s.plant.origins[c],s.traces[c],t)
            if abs(x-s.plant.locations[station])>1e-5 or abs(v)>1e-5:
                raise ValueError(f"{k} 取放点位置或停车速度不符")
        if not next_job and (s.casts[k]+EPS<b or (j.cast_time and s.casts[k]+EPS<j.due)):
            raise ValueError(f"{k} 连铸开工早于交付或计划")
        if j.cast_prev and s.casts[k]+EPS<s.casts[j.cast_prev]+s.jobs[j.cast_prev].cast_time:
            raise ValueError(f"{k} 连铸交接重叠")
    for c, pairs in intervals.items():
        ordered=sorted(pairs)
        if any(y[0]<x[1]-EPS for x,y in zip(ordered,ordered[1:])):
            raise ValueError(f"{c} 同车任务重叠")
    for name, pairs in slots.items():
        changes = sorted((t,d) for a,b in pairs if b>a+EPS for t,d in ((a,1),(b,-1)))
        occupied = 0
        for _,delta in changes:
            occupied += delta
            if occupied>s.plant.capacity[name]:
                raise ValueError(f"{name} 超容量")
    traces = list(zip(s.plant.origins,s.traces))
    gap = min((min_gap(x,y) for x,y in zip(traces,traces[1:])),default=inf)
    if gap+EPS<s.plant.rail.gap:
        raise ValueError("连续轨迹违反最小间距")
    return gap


# 作用：汇总包等车、车等包、接收位等待、连铸风险与轨道运行指标。
# 参数：s 为通过审计的完整终态，target 为选作展示的目标任务编号。
# 返回：指标字典，时间单位秒，概率为 0～1，无可核对交接时断浇数为 None。
def metrics(s, target):
    gap = audit(s)
    waits = {k:s.pick[k]-s.ready[k] for k in s.jobs}
    finals = [j for j in s.plant.jobs if j.key not in s.child and j.cast_time>0]
    late = [max(0,s.drop[j.key]-j.due) for j in finals]
    linked = [j for j in finals if j.cast and j.cast_prev and s.jobs[j.cast_prev].cast==j.cast
              and j.cast_sequence==s.jobs[j.cast_prev].cast_sequence+1]
    gaps = [max(0,s.drop[j.key]-(s.casts[j.cast_prev]+s.jobs[j.cast_prev].cast_time)) for j in linked]
    unavoidable=[]
    for j in finals:
        leg=template(s.plant.rail,s.plant.locations[j.source],s.plant.locations[j.dest],j.load)
        earliest=s.ready[j.key]+(leg[-1].end if leg else 0.)
        if earliest>j.due+EPS:
            unavoidable.append(j.key)
    moved = sum(b-a for _,_,_,a,b in s.logs)
    return dict(tasks=len(waits),wait_total=sum(waits.values()),wait_mean=fmean(waits.values()),
                wait_p95=quantile(list(waits.values()),.95),target_wait=waits[target],
                other_wait=sum(v for k,v in waits.items() if k!=target),
                car_wait=sum(max(0,s.ready[k]-s.arrival[k]) for k in waits),
                post_drop=sum((s.starts[s.child[k].key] if k in s.child else s.casts[k])-v
                              for k,v in s.drop.items()),late_count=sum(x>EPS for x in late),
                cast_deliveries=len(late),late_rate=fmean(x>EPS for x in late) if late else None,
                late_seconds=sum(late),linked_handoffs=len(gaps),
                modeled_breaks=sum(x>EPS for x in gaps) if gaps else None,
                observed_breaks=None,empty_seconds=sum(b-a for _,_,l,a,b in s.logs if l!='loaded'),
                move_seconds=moved,conditional_unavoidable_jobs=unavoidable,
                crane_reserved_seconds=sum(b-s.dispatch_at[k] for k,b in s.drop.items()),
                yield_seconds=sum(b-a for _,_,l,a,b in s.logs if l=='yield'),
                min_gap=gap if gap!=inf else None,violations=0,
                repair_count=len(s.repairs),repair_seconds=sum(s.repairs.values()),
                decision_seconds=s.decision_seconds)
