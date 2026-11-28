"""事件驱动执行器。策略接口不接触外生真实时长。"""
from math import inf, isfinite
from time import perf_counter
from model.motion.track import EPS
from model.dispatch.policy import candidates, estimate, predicted, corridor
from model.replay.state import Order


# 作用：执行首个发车或邻位待命指令。
# 参数：s 为现场，order 为首步指令，p 为策略（控制估时）。
# 返回：无；轨迹通过复核才提交；原地暂缓由事件循环唤醒。
def apply(s, order, p):
    if order.wake is not None:
        s.holds[order.car] = order.wake
        return
    c, k = order.car,order.job
    j = s.jobs[k]
    src = s.plant.locations[j.source]
    dest = src if order.via is None else order.via
    x = s.pos[c]
    route = s.route(c,(dest,),0,s.now)
    if route is None:
        raise ValueError("提交的空驶指令存在轨道冲突")
    planned = estimate(s,x,dest,0,p.estimate)
    s.move(c,dest,route,'empty' if order.via is None else 'standby',k)
    if route[1]>s.now+planned+EPS:
        s.repairs[(k,'approach',order.via)] = route[1]-s.now-planned
    s.dispatch_at[k] = s.now
    s.owner[k] = c
    s.assigned[c] = k
    if order.via is None:
        s.arrival[k] = route[1]


# 作用：处理截至当前的落包、开工、完工、待命离位、起吊和连铸交接事件。
# 参数：s 为现场，world 为执行器私有的工时实现，p 为估时和离位规则。
# 返回：无；同刻零时长事件持续处理直至稳定。
def settle(s, world, p):
    for _ in range(8*len(s.jobs)+10):
        changed = False
        for j in s.plant.jobs:
            k = j.key
            if k in s.due_drop and k not in s.drop and s.due_drop[k]<=s.now+EPS:
                s.drop[k] = s.now
                del s.assigned[s.owner[k]]
                changed = True
            if k not in s.starts:
                if j.prev:
                    available = j.prev in s.drop
                else:
                    available = j.release<=s.now+EPS and s.has_slot(j.source)
                if available:
                    s.starts[k] = -j.initial_elapsed if j.initial_elapsed else s.now
                    changed = True
            if k in s.starts and k not in s.ends and s.starts[k]+world[k]<=s.now+EPS:
                s.ends[k] = s.starts[k]+world[k]
                changed = True
            if k in s.ends and k not in s.ready and s.ends[k]+j.prep<=s.now+EPS:
                s.ready[k] = s.ends[k]+j.prep
                changed = True
            if k in s.owner and k not in s.arrival:
                car = s.owner[k]
                src = s.plant.locations[j.source]
                stop_end = s.traces[car][-1].end if s.traces[car] else s.now
                travel = estimate(s,s.pos[car],src,0,p.estimate)
                if max(stop_end,predicted(s,j,p.q)-travel)<=s.now+EPS:
                    route = s.route(car,(src,),0,s.now)
                    if route:
                        s.move(car,src,route,'empty',k)
                        s.arrival[k] = route[1]
                        if route[1]>s.now+travel+EPS:
                            s.repairs[k,'last_mile'] = route[1]-s.now-travel
                        changed = True
            if (k in s.ready and s.arrival.get(k,inf)<=s.now+EPS
                    and k not in s.pick and s.accepts(j)):
                c = s.owner[k]
                dest = s.plant.locations[j.dest]
                route = s.route(c,(dest,),j.load,s.now)
                if route:
                    planned = estimate(s,s.pos[c],dest,j.load,p.estimate)
                    s.pick[k] = s.now
                    s.move(c,dest,route,'loaded',k)
                    s.due_drop[k] = route[1]
                    if route[1]>s.now+planned+EPS:
                        s.repairs[k,'loaded'] = route[1]-s.now-planned
                    changed = True
            if k in s.drop and k not in s.child and k not in s.casts:
                prev_end = -inf
                if j.cast_prev:
                    prev_end = s.casts.get(j.cast_prev,inf)+s.jobs[j.cast_prev].cast_time
                if max(j.due if j.cast_time else 0.,prev_end)<=s.now+EPS:
                    s.casts[k] = s.now
                    changed = True
        if not changed:
            return
    raise RuntimeError("同刻事件未收敛")


# 作用：求下一次真实事件或策略计时器到时，驱动仿真时钟。
# 参数：s 为现场，world 为执行器工时，wake 为策略主动重评时间，p 为策略。
# 返回：下一事件绝对时刻；无事件返回无穷。
def next_time(s, world, wake, p):
    times = [wake]
    for _,a,b in s.plant.bookings:
        times.extend((a,b))
    for j in s.plant.jobs:
        k = j.key
        if k not in s.starts:
            times.append(j.release)
        elif k not in s.ends:
            times.extend((s.starts[k]+world[k],s.starts[k]+
                          (int((s.now-s.starts[k]+EPS)/p.progress)+1)*p.progress))
        elif k not in s.ready:
            times.append(s.ends[k]+j.prep)
        if k in s.owner and k not in s.arrival:
            car = s.owner[k]
            travel = estimate(s,s.pos[car],s.plant.locations[j.source],0,p.estimate)
            times.append(predicted(s,j,p.q)-travel)
        if k in s.drop and k not in s.casts and k not in s.child:
            times.append(j.due)
            if j.cast_prev in s.casts:
                times.append(s.casts[j.cast_prev]+s.jobs[j.cast_prev].cast_time)
    times.extend(s.arrival.values())
    times.extend(s.due_drop.values())
    times.extend(tr[-1].end for tr in s.traces if tr)
    return min((t for t in times if t>s.now+EPS),default=inf)


# 作用：在取放通道受静止邻车阻挡时尝试向外侧合法停车位避让。
# 参数：s 为现场，只处理尚无已派车或在途指令的阻挡车辆。
# 返回：成功提交一次避让时为 True，否则为 False。
def yield_neighbor(s):
    for j in s.plant.jobs:
        if j.key not in s.starts or j.key in s.owner or not s.accepts(j):
            continue
        src,dst = s.plant.locations[j.source],s.plant.locations[j.dest]
        for c in j.eligible:
            if c in s.assigned or c in s.holds:
                continue
            for n,direction in ((c-1,-1),(c+1,1)):
                if not 0<=n<len(s.pos) or n in s.assigned or n in s.holds:
                    continue
                if s.traces[n] and s.traces[n][-1].end>s.now+EPS:
                    continue
                edge = min(src,dst,s.pos[c]) if direction<0 else max(src,dst,s.pos[c])
                if direction*(s.pos[n]-edge)>=s.plant.rail.gap-EPS:
                    continue
                parks = sorted(s.plant.rail.parking,key=lambda x:abs(x-s.pos[n]))
                for x in parks:
                    if direction*(x-edge)<s.plant.rail.gap or not corridor(s,n,(s.pos[n],x)):
                        continue
                    route = s.route(n,(x,),0,s.now)
                    if route and route[0]:
                        s.move(n,x,route,'yield',j.key)
                        return True
    return False


# 作用：在完整批次上执行滚动策略，每次只提交首步。
# 参数：s 为启动现场，world 为按任务固定的外生工时，p 为策略，selector 为可选配对回放选择器。
# 返回：终态 State，无法完成时 blocked 说明原因。
def run(s, world, p, selector=None):
    if set(world)!=set(s.jobs) or any(not isfinite(d) or d<0 for d in world.values()):
        raise ValueError("场景工时缺失或非法")
    if any(j.initial_elapsed and world[j.key]<=j.initial_elapsed for j in s.plant.jobs):
        raise ValueError("场景与窗首在制观测矛盾")
    for _ in range(3000*max(1,len(s.jobs))):
        settle(s,world,p)
        if len(s.drop)==len(s.jobs) and all(j.key in s.casts for j in s.plant.jobs if j.key not in s.child):
            return s
        start = perf_counter()
        options,wake = candidates(s,p,expanded=selector is not None)
        if options:
            choice = selector(s,p,options) if selector else options[0]
            apply(s,choice,p)
            s.decision_seconds += perf_counter()-start
            continue
        elif not isfinite(wake) and yield_neighbor(s):
            s.decision_seconds += perf_counter()-start
            continue
        s.decision_seconds += perf_counter()-start
        t = next_time(s,world,wake,p)
        if not isfinite(t):
            s.blocked = '工位、共轨位置或窗外预约阻塞'
            return s
        s.now = t
        s.holds.clear()  # 到达新事件后，各车用当时的观测重新决策
    s.blocked = '超出事件步数上限'
    return s
