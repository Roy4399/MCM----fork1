"""各算法共享的只读问题描述。时间为秒，距离为米。"""
from dataclasses import dataclass
from math import inf, isfinite
from model.motion.track import Rail


@dataclass(frozen=True)
class Job:
    key: str
    heat: str
    kind: str
    source: str
    dest: str
    prev: str | None
    release: float    # 首段最早开工，后段由上一段落包触发
    mean: float
    prep: float
    planned_ready: float
    due: float        # 工艺交接参考，终段为开浇期限
    eligible: tuple   # 零起点的行车索引
    load: int = 2
    cast_time: float = 0.
    cast: str = ''
    cast_prev: str | None = None
    cast_sequence: int = 0
    initial_elapsed: float = 0.  # 窗首在制，已开工但未结束
    ferry_return: bool = False    # 空包回程最后一段须进渡车
    commitment: float = inf      # 可选硬性窗外最晚交付时间


@dataclass(frozen=True)
class Plant:
    jobs: tuple
    rail: Rail
    origins: tuple
    locations: dict
    capacity: dict
    bookings: tuple = ()  # (资源名, 开始, 结束)，半开区间
    average_speed: float = 1.0
    heavy_eligible: tuple = ()
    ferries: tuple = ()
    window_open: float | None = None
    window_close: float | None = None
    next_commitment: str | None = None


# 作用：校验任务图、设备节点、车辆资格及外部预约，拒绝缺少承接段的工艺图。
# 参数：p 为 Plant，包括按工艺拓扑顺序录入的任务。
# 返回：无；校验失败抛出 ValueError。
def validate(p):
    seen, parents = {}, set()
    r = p.rail
    if (tuple(sorted(set(r.nodes))) != r.nodes or len(r.limits) != len(r.nodes)-1
            or min(r.limits) <= 0 or r.gap <= 0 or len(r.profiles)!=3
            or any(min(x)<=0 for x in r.profiles) or p.average_speed<=0):
        raise ValueError("轨道边界或运动参数错误")
    if r.gears and (len(r.gears)!=3 or any(not g or tuple(sorted(g))!=g or min(g)<=0 for g in r.gears)
                    or len(r.edge_gears)!=len(r.limits)
                    or any(k<0 or any(k>=len(g) for g in r.gears) for k in r.edge_gears)):
        raise ValueError("档速或区段档号非法")
    if any(x not in r.nodes for x in (*p.origins,*r.parking,*p.locations.values())):
        raise ValueError("位置须为轨道节点")
    if any(y-x<r.gap for x,y in zip(p.origins,p.origins[1:])):
        raise ValueError("初始车距不足")
    for j in p.jobs:
        if j.key in seen or not j.eligible or any(c<0 or c>=len(p.origins) for c in j.eligible):
            raise ValueError("重复任务或车辆资格错误")
        if (not all(isfinite(v) for v in (j.mean,j.prep,j.due,j.release,j.planned_ready,j.cast_time))
                or min(j.mean,j.prep,j.cast_time,j.initial_elapsed)<0 or j.load not in (1,2)
                or j.initial_elapsed and (j.prev or j.release>0)):
            raise ValueError("工时或窗首状态错误")
        if p.heavy_eligible and j.load==2 and not set(j.eligible)<=set(p.heavy_eligible):
            raise ValueError("重包任务含不合格行车")
        if j.ferry_return and (j.load!=1 or j.dest not in p.ferries):
            raise ValueError("空包回程目的地必须为允许的渡车")
        if j.prev:
            pred = seen.get(j.prev)
            if not pred or pred.dest != j.source or pred.heat != j.heat or j.prev in parents:
                raise ValueError("工艺链断开或分叉")
            parents.add(j.prev)
        for s in (j.source,j.dest):
            if s not in p.locations or p.capacity.get(s,0)<1:
                raise ValueError("缺少工位或容量")
        seen[j.key]=j
    for j in p.jobs:
        if j.cast_prev:
            k=seen.get(j.cast_prev)
            if not k or k.dest!=j.dest or k.due>=j.due or k.key in parents:
                raise ValueError("浇铸先后关系错误")
    for s,a,b in p.bookings:
        if s not in p.capacity or b<=a:
            raise ValueError("外部预约非法")
