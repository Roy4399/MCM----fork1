"""首步生成与非预知的滚动派车规则。"""
from dataclasses import dataclass
from math import inf
from model.forecast.duration import quantile
from model.motion.track import EPS, template
from model.replay.state import Order


@dataclass(frozen=True)
class Policy:
    timing: str = 'quantile'
    scope: str = 'rule'
    estimate: str = 'geared'
    q: float = .25
    advance: float = 120.
    draws: int = 2
    risk: float | None = None
    progress: float = 120.


# 作用：计算一次行程的计划用时；执行器始终使用分档模型。
# 参数：state 为现场，x/y 为起终坐标，load 为负载档，method 为 average 或 geared。
# 返回：估计秒数。
def estimate(state, x, y, load, method):
    if method == 'average':
        return abs(y-x)/state.plant.average_speed
    leg = template(state.plant.rail,x,y,load)
    return leg[-1].end if leg else 0.


# 作用：计算当前工序的条件可吊分位时刻。
# 参数：s 为现场，j 为任务，q 为分位水平。
# 返回：绝对可吊时刻（秒）。
def predicted(s, j, q):
    k = j.key
    if k in s.ends:
        return s.ends[k]+j.prep
    i = s.forecast.index[k]
    rows = s.forecast.belief(j.heat,s.now,s.starts,s.ends)
    return s.starts[k]+quantile([r[i] for r in rows],q)+j.prep


# 作用：检查新行动与其他已派车辆预留的取放区间是否相容。
# 参数：s 为现场，c 为候选车，points 为当前动作从起点至终点会经过的坐标。
# 返回：布尔值；此保守预留在落包后解除。
def corridor(s, c, points):
    for n,k in s.assigned.items():
        if n==c:
            continue
        j = s.jobs[k]
        other = [s.pos[n],s.plant.locations[j.dest]]
        if k not in s.pick:
            other.append(s.plant.locations[j.source])
        gap = min(other)-max(points) if c<n else min(points)-max(other)
        if gap+EPS<s.plant.rail.gap:
            return False
    return True


# 作用：枚举可执行首步，包括原地暂缓、去取包、邻位待命及先接另一包。
# 参数：s 为可见状态，p 为策略，expanded 控制是否枚举待命变体。
# 返回：(依优先级排序的 Order 列表，下一次主动唤醒时刻)。
def candidates(s, p, expanded=False):
    options, wake = [], min(s.holds.values(),default=inf)
    for j in s.plant.jobs:
        k = j.key
        if k not in s.starts or k in s.owner:
            continue
        if j.cast_prev and j.cast_prev not in s.drop:
            continue
        if not s.accepts(j):
            continue
        src,dst = (s.plant.locations[n] for n in (j.source,j.dest))
        for c in j.eligible:
            if c in s.assigned or c in s.holds or (s.traces[c] and s.traces[c][-1].end>s.now+EPS):
                continue
            via_points = [None]
            if expanded:
                via_points += sorted((x for x in s.plant.rail.parking if x not in (s.pos[c],src)),
                                     key=lambda x:abs(x-src))[:1]
            for via in via_points:
                stops = (src,) if via is None else (via,src)
                path = (s.pos[c],)+stops
                travel = sum(estimate(s,x,y,0,p.estimate) for x,y in zip(path,path[1:]))
                physical = sum(estimate(s,x,y,0,'geared') for x,y in zip(path,path[1:]))
                target = predicted(s,j,p.q) if p.timing=='quantile' else j.planned_ready
                if p.timing=='fixed':
                    target -= p.advance
                if k in s.ready and p.timing=='quantile':
                    target = min(target,s.now)
                depart = max(s.now,target-travel)
                if depart>s.now+EPS:
                    wake = min(wake,depart)
                    if not expanded:
                        continue
                if not corridor(s,c,(*path,dst)):
                    continue
                if not s.route(c,stops,0,s.now):
                    continue
                # 不可越过静止邻车；尚在移动的邻车由实际起吊时再精确复核。
                if any((dst-s.pos[n] if n<c else s.pos[n]-dst)<s.plant.rail.gap-EPS
                       for n in (c-1,c+1) if 0<=n<len(s.pos)):
                    continue
                priority = (min(j.due,j.commitment), k not in s.ready, depart, physical,c,k)
                # 去邻位时可立即占位，离开邻位的时刻由新观测决定。
                if expanded and via is not None:
                    options.append((priority,Order(c,k,via,s.now)))
                elif depart<=s.now+EPS or expanded:
                    options.append((priority,Order(c,k,None,s.now)))
                if expanded and depart>s.now+EPS and via is None:
                    options.append((priority,Order(c,k,wake=depart)))
    options.sort(key=lambda item:item[0])
    return [o for _,o in options], wake
