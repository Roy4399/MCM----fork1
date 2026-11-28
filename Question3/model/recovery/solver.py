from itertools import chain
from math import ceil
from time import perf_counter
from model.domain.types import Trip
from model.motion.rail import move,safe
from model.recovery.orders import bounded_orders,compressed


# 作用：比较全计划。参数：trips 派工记录，tasks 任务名映射。返回：备包迟到、待吊、实际移动、改派、时刻偏移的字典序目标。
def score(trips,tasks):
    return (sum(max(0,tr.end-tasks[tr.task].due) for tr in trips),
            sum(tr.start-tasks[tr.task].ready for tr in trips),
            round(sum(tr.moving for tr in trips),8),
            sum(tr.crane!=tasks[tr.task].crane for tr in trips),
            sum(abs(tr.start-tasks[tr.task].original) for tr in trips))


# 作用：求解小窗口恢复排程。参数：case 当前快照，policy 为 original/fill/cps，active 可调名集合，k 排名偏移界，seconds 搜索时限，first 可选的共同首单及起吊时刻。返回：已校验排程、目标及搜索状态。
def solve(case,policy,active,k=1,seconds=3,first=None):
    start_clock = perf_counter()
    tasks = {t.name:t for t in case.tasks}
    order = tuple(sorted(case.tasks,key=lambda t:t.original))
    if len(tasks)!=len(order) or policy not in ('original','fill','cps'):
        raise ValueError('重复任务名或非法策略')
    orders = [order]
    if policy!='original':
        orders.append(compressed(order,active,case))
    if policy=='cps':
        orders=chain(orders,bounded_orders(order,active,k))
    best = None
    best_trips = ()
    timed_out = False

    # 作用：搜索候选的指派、起吊时刻及进位轨迹。参数：seq 排序后的任务，trips 已派前缀，ends 已排任务交付映射，bookings 工位占用区间。返回：无，更新外层最优已验证解。
    def visit(seq,trips,ends,bookings):
        nonlocal best,best_trips,timed_out
        if perf_counter()-start_clock>seconds:
            timed_out=True
            return
        current = score(trips,tasks)
        if best is not None and current>=best:
            return
        if len(trips)==len(seq):
            if safe(case.cranes,trips,case.now,case.horizon,case.gap):
                best,best_trips=current,trips
            return
        t=seq[len(trips)]
        if not trips and first and t.name!=first[0]:
            return
        if any(p not in ends for p in t.parents):
            return
        fixed=t.locked or t.name not in active
        for c in case.cranes:
            if c.name not in t.eligible or c.capacity<t.weight or (fixed or policy!='cps') and c.name!=t.crane:
                continue
            prior=next((r for r in reversed(trips) if r.crane==c.name),None)
            available=prior.end if prior else case.now
            position=tasks[prior.task].destination if prior else c.position
            empty,travel=move(position,t.origin,c.empty_speed,c.acceleration,0)
            lower=ceil(max([available+travel,t.ready]+[ends[p] for p in t.parents])-1e-9)
            if policy=='original':
                lower=max(lower,t.original)
            starts=(t.original,) if fixed else range(lower,case.horizon+1)
            for s in starts:
                if s<lower or (not trips and first and s!=first[1]):
                    continue
                load,arrival=move(t.origin,t.destination,c.loaded_speed,c.acceleration,s)
                end=ceil(arrival-1e-9)+t.service
                if end>case.horizon:
                    break
                if t.station and any(st==t.station and a<b and ((end<b and a<end+t.dwell) if t.dwell else a<=end<b) for st,a,b in bookings):
                    continue
                # 空驶默认尽早或贴近取包时刻；中间整数出发时刻允许让行。
                departures=[available] if not empty else sorted({available,s-travel,*range(available, int(s-travel)+1)})
                for dep in departures:
                    motion=tuple((a+dep,b+dep,x,v,g) for a,b,x,v,g in empty)+load
                    trip=Trip(t.name,c.name,s,end,motion,travel+arrival-s)
                    visit(seq,trips+(trip,),dict(ends,**{t.name:end}),bookings+((t.station,end,end+t.dwell),))
                    if timed_out:
                        return
    for seq in orders:
        visit(seq,(),{},case.outages)
        if timed_out:
            break
    return {'trips':best_trips,'score':best,'status':'timeout' if timed_out else ('feasible' if best is not None else 'infeasible')}
