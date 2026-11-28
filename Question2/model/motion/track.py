"""一维共轨的分段运动、连续间距校验。输入单位在一次调用内保持一致。"""
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from math import sqrt, inf

EPS = 1e-8


@dataclass(frozen=True)
class Phase:
    start: float
    end: float
    x: float
    v: float
    a: float


# 作用：复原某一运动相位的瞬时状态。
# 参数：p 为常加速度相位，t 为相位内的绝对时间。
# 返回：(位置，速度，加速度)。
def evaluate(p, t):
    dt = t - p.start
    return p.x + dt*p.v + p.a*dt*dt/2, p.v + p.a*dt, p.a


# 作用：计算单个正向区段的加速、匀速、制动时间。
# 参数：length 为长度，u/w 为首末速率，vmax 为速限，a/b 为加减速度大小。
# 返回：依次为加速、匀速、减速的持续时间。
def segment(length, u, w, vmax, a, b):
    if min(length, vmax, a, b) <= 0 or min(u, w) < 0 or max(u, w) > vmax + EPS:
        raise ValueError("区段或边界速度非法")
    peak = min(vmax, sqrt((2*a*b*length + b*u*u + a*w*w)/(a+b)))
    if peak + EPS < max(u, w):
        raise ValueError("边界速度不可达")
    cruise = length - (peak*peak-u*u)/(2*a) - (peak*peak-w*w)/(2*b)
    return max(0, (peak-u)/a), (cruise/peak if cruise > EPS*length else 0.), max(0, (peak-w)/b)


@dataclass(frozen=True)
class Rail:
    nodes: tuple
    limits: tuple
    profiles: tuple  # 每种负载的 (档速上限，加速度，制动减速度)
    parking: tuple
    gap: float
    gears: tuple = ()  # 按负载索引的低、中、高档速
    edge_gears: tuple = ()  # 区段允许的最高档号，0 为最低档


# 作用：在各限速边界做双向速度传播，生成静止起止的单向行程模板。
# 参数：rail 为轨道，src/dst 为节点坐标，load 为负载档索引（0 空车、1 空包、2 重包）。
# 返回：起始时间为零的 Phase 元组，反向行驶带负速度。
@lru_cache(maxsize=8192)
def template(rail, src, dst, load):
    i, j = rail.nodes.index(src), rail.nodes.index(dst)
    if i == j:
        return ()
    sign = 1 if j > i else -1
    points = [rail.nodes[k] for k in range(i, j+sign, sign)]
    gear, a, b = rail.profiles[load]
    caps = [min(gear, rail.limits[min(k, k+sign)]) for k in range(i, j, sign)]
    if rail.gears:
        caps = [min(v, rail.gears[load][rail.edge_gears[min(k,k+sign)]])
                for v,k in zip(caps,range(i,j,sign))]
    lengths = [abs(y-x) for x, y in zip(points, points[1:])]
    speeds = [0.] + [min(x,y) for x,y in zip(caps, caps[1:])] + [0.]
    for k, length in enumerate(lengths):
        speeds[k+1] = min(speeds[k+1], sqrt(speeds[k]**2 + 2*a*length))
    for k in reversed(range(len(lengths))):
        speeds[k] = min(speeds[k], sqrt(speeds[k+1]**2 + 2*b*lengths[k]))
    parts, time = [], 0.
    for k, length in enumerate(lengths):
        x, v = points[k], sign*speeds[k]
        times = segment(length, speeds[k], speeds[k+1], caps[k], a, b)
        for dt, accel in zip(times, (sign*a, 0., -sign*b)):
            if dt > EPS:
                phase = Phase(time, time+dt, x, v, accel)
                parts.append(phase)
                time += dt
                x, v, _ = evaluate(phase, time)
    return tuple(parts)


# 作用：读取含静止间隔的轨迹，空白区间表示停车。
# 参数：origin 是初始位置，trace 为已按时间排序的相位序列，t 为查询时间。
# 返回：(位置，速度，加速度)，边界采用右侧状态。
def state_at(origin, trace, t):
    k = bisect_right(trace, t, key=lambda p: p.start) - 1
    if k < 0:
        return origin, 0., 0.
    p = trace[k]
    if t >= p.end:
        return evaluate(p, p.end)[0], 0., 0.
    return evaluate(p, t)


# 作用：精确求两车从指定时刻到全部承诺结束的最小间距。
# 参数：left/right 均为 (初位，轨迹)；since 为检查起点。
# 返回：相邻右车位置减左车位置的全程最小值，可能为负。
def min_gap(left, right, since=0.):
    times = sorted({since} | {t for _, trace in (left,right) for p in trace
                              for t in (p.start,p.end) if t >= since})
    minimum = inf
    for i, t in enumerate(times):
        x, v, a = state_at(*left, t)
        y, w, b = state_at(*right, t)
        minimum = min(minimum, y-x)
        if i+1 < len(times) and abs(b-a) > EPS:
            dt = (v-w)/(b-a)
            if 0 < dt < times[i+1]-t:
                minimum = min(minimum, y-x+(w-v)*dt+(b-a)*dt*dt/2)
    return minimum


# 作用：将模板时移为绝对时刻轨迹。
# 参数：pieces 为零起点的相位元组，start 为发车时刻。
# 返回：绝对时间相位元组。
def shifted(pieces, start):
    return tuple(Phase(p.start+start,p.end+start,p.x,p.v,p.a) for p in pieces)
