from math import sqrt


# 作用：生成停车至停车的分档限速移动。参数：x/y 起终坐标，v 档速上限，a 加减速度，t 出发时刻。返回：运动段元组、到达时刻。
def move(x, y, v, a, t):
    if v <= 0 or a <= 0:
        raise ValueError('速度与加速度须为正')
    distance = abs(y - x)
    speed = min(v, sqrt(distance * a))
    direction = 1 if y >= x else -1
    parts = []
    for duration, velocity, accel in (
        (speed/a, 0, direction*a),
        (max(0, distance-speed*speed/a)/v, direction*speed, 0),
        (speed/a, direction*speed, -direction*a),
    ):
        if duration > 1e-10:
            parts.append((t, t+duration, x, velocity, accel))
            x += velocity*duration + accel*duration**2/2
            t += duration
    return tuple(parts), t


# 作用：查询含驻车的连续轨迹。参数：parts 按时序排列的运动段，initial 初位，t 时刻。返回：位置、速度、加速度。
def state(parts, initial, t):
    x = initial
    for begin, end, origin, speed, accel in parts:
        if t < begin:
            break
        dt = min(t,end)-begin
        x = origin + speed*dt + accel*dt**2/2
        if t < end:
            return x, speed+accel*dt, accel
    return x, 0, 0


# 作用：核验相邻车整个时域的间距。参数：cranes 按空间排序的车辆，trips 派工集合，now/horizon 边界，gap 最小间距。返回：是否全程合规。
def safe(cranes, trips, now, horizon, gap):
    paths = {c.name: sorted(seg for tr in trips if tr.crane == c.name for seg in tr.motion) for c in cranes}
    for left, right in zip(cranes, cranes[1:]):
        lp, rp = paths[left.name], paths[right.name]
        cuts = sorted({now,horizon} | {v for seg in lp+rp for v in seg[:2]})
        for lo, hi in zip(cuts,cuts[1:]):
            mid = (lo+hi)/2
            x,v,a = state(lp,left.position,mid)
            y,w,b = state(rp,right.position,mid)
            times = [lo-mid,hi-mid]
            if abs(b-a)>1e-10:
                root = (v-w)/(b-a)
                if times[0]<root<times[1]:
                    times.append(root)
            if min(y-x+(w-v)*t+(b-a)*t*t/2 for t in times) < gap-1e-8:
                return False
    return True
