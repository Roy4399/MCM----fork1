from functools import lru_cache


# 作用：用子集动态规划构造有限偏移的候选序列。参数：tasks 原预约序列，active 可调整任务名，k 最大名次变化。返回：满足工艺前驱与排名界的序列元组，适合小窗口。
def bounded_orders(tasks, active, k):
    ranks = {t.name:sum(p.crane==t.crane for p in tasks[:i]) for i,t in enumerate(tasks)}
    # 作用：缓存同一已选子集的合法后继。参数：done 已排序任务集合。返回：可接续任务元组。
    @lru_cache(None)
    def successors(done):
        possible=[]
        for t in tasks:
            bound=k if t.name in active and not t.locked else 0
            place=sum(p.crane==t.crane and p.name in done for p in tasks)
            if t.name not in done and abs(ranks[t.name]-place)<=bound and set(t.parents)<=done:
                possible.append(t)
        return tuple(possible)

    # 作用：按需遍历子集图，避免预生成全部排列。参数：prefix 已排前缀，done 已排名字。返回：合法的跨车交织序列迭代器。
    def walk(prefix,done):
        if len(prefix)==len(tasks):
            yield prefix
        else:
            for t in successors(done):
                yield from walk(prefix+(t,),done|{t.name})
    return walk((),frozenset())


# 作用：在原预约空档内顺次回填。参数：tasks 原序，active 可调整集合，case 现场车辆。返回：回填后的派工优先序。
def compressed(tasks, active, case):
    from model.motion.rail import move
    order = list(tasks)
    for i in range(len(order)):
        head = order[i]
        if head.name not in active or head.ready<=head.original or head.locked:
            continue
        vehicle = next(c for c in case.cranes if c.name==head.crane)
        previous = [t for t in order[:i] if t.crane==head.crane]
        pos = previous[-1].destination if previous else vehicle.position
        for j in range(i+1,len(order)):
            t = order[j]
            if t.crane!=head.crane or t.locked or t.name not in active:
                continue
            if not set(t.parents)<={p.name for p in order[:i]}:
                continue
            arrival = move(pos,t.origin,vehicle.empty_speed,vehicle.acceleration,head.original)[1]
            end = move(t.origin,t.destination,vehicle.loaded_speed,vehicle.acceleration,max(arrival,t.ready))[1]+t.service
            back = move(t.destination,head.origin,vehicle.empty_speed,vehicle.acceleration,end)[1]
            if back<=t.original and end<=head.original+head.service+move(head.origin,head.destination,vehicle.loaded_speed,vehicle.acceleration,0)[1]+1e-8:
                order.insert(i,order.pop(j))
                break
    return tuple(order)
