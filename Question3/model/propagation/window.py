from math import ceil
from model.motion.rail import move


# 作用：沿原预约的先后与资源依赖传播超出余量的延误。参数：case 现场快照，neighbors 是否纳入邻轨筛查。返回：受影响任务名集合。
def affected(case, neighbors=True):
    ordered = sorted(case.tasks, key=lambda task: task.original)
    fleet = {c.name:i for i,c in enumerate(case.cranes)}
    delay = {t.name:max(0,t.ready-t.original) for t in ordered}
    for i,t in enumerate(ordered):
        if not delay[t.name]:
            continue
        crane = case.cranes[fleet[t.crane]]
        loaded = move(t.origin,t.destination,crane.loaded_speed,crane.acceleration,0)[1]
        release = t.original + ceil(loaded-1e-9) + t.service
        for u in ordered[i+1:]:
            limits = []
            if t.name in u.parents:
                limits.append(release)
            if t.crane == u.crane:
                travel = move(t.destination,u.origin,crane.empty_speed,crane.acceleration,0)[1]
                limits.append(release+travel)
            if t.station and t.station == u.station:
                limits.append(release+t.dwell)
            adjacent = abs(fleet[t.crane]-fleet[u.crane]) == 1
            separation = max(min(t.origin,t.destination),min(u.origin,u.destination))-min(max(t.origin,t.destination),max(u.origin,u.destination))
            if neighbors and adjacent and separation < case.gap:
                limits.append(release)
            if limits:
                delay[u.name] = max(delay[u.name],ceil(max(limits)+delay[t.name]-u.original-1e-9))
    return {key for key,value in delay.items() if value}
