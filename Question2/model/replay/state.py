"""只保存现场可见信息与已提交指令；真实场景工时由执行器单独持有。"""
from copy import copy
from dataclasses import dataclass, field
from math import inf
from model.motion.track import EPS, shifted, template, min_gap


@dataclass(frozen=True)
class Order:
    car: int
    job: str
    via: float | None = None
    depart: float = 0.
    wake: float | None = None     # 原地暂缓动作的最晚重新评价时刻


@dataclass
class State:
    plant: object
    forecast: object
    now: float = 0.
    starts: dict = field(default_factory=dict)
    ends: dict = field(default_factory=dict)
    ready: dict = field(default_factory=dict)
    pick: dict = field(default_factory=dict)
    drop: dict = field(default_factory=dict)
    casts: dict = field(default_factory=dict)
    owner: dict = field(default_factory=dict)
    dispatch_at: dict = field(default_factory=dict)
    rankings: list = field(default_factory=list)
    arrival: dict = field(default_factory=dict)
    due_drop: dict = field(default_factory=dict)
    holds: dict = field(default_factory=dict)
    assigned: dict = field(default_factory=dict)
    traces: list = field(default_factory=list)
    pos: list = field(default_factory=list)  # 每车已提交轨迹的终点
    logs: list = field(default_factory=list)
    repairs: dict = field(default_factory=dict)
    decision_seconds: float = 0.
    blocked: str = ''

    # 作用：建立路线索引、车辆初位及空轨迹。
    # 参数：self 为新建现场状态。
    # 返回：无；更新实例索引。
    def __post_init__(self):
        self.jobs = {j.key:j for j in self.plant.jobs}
        self.child = {j.prev:j for j in self.plant.jobs if j.prev}
        self.pos = list(self.plant.origins)
        self.traces = [() for _ in self.pos]

    # 作用：复制可见现场，在各首步反事实间隔离写入。
    # 参数：self 为当前状态。
    # 返回：独立的 State，共享只读问题、预测器与不可变相位。
    def fork(self):
        other = copy(self)
        for name,value in vars(self).items():
            if isinstance(value,(dict,list)) and name not in ('jobs','child'):
                setattr(other,name,value.copy())
        return other

    # 作用：统计工位当前占用和在途预约。
    # 参数：self 为现场，name 为工位名，at 为查询时刻（当前时刻或外部预约边界）。
    # 返回：占用数，当前未释放的钢包在查询时保守视为继续占位。
    def occupancy(self, name, at):
        count = sum(s==name and a<=at<b for s,a,b in self.plant.bookings)
        for j in self.plant.jobs:
            k = j.key
            if not j.prev and j.source==name and k in self.starts and k not in self.pick:
                count += 1
            if j.dest==name and k in self.pick:
                next_job = self.child.get(k)
                if (next_job and next_job.key not in self.pick) or (not next_job and k not in self.casts):
                    count += 1
        return count

    # 作用：核验从当前时刻至确定释放前的区间容量。
    # 参数：self 为现场，name 为工位，until 为保守占位结束（未知时为无穷）。
    # 返回：布尔值，已有占用和外部预约能否再容纳一个钢包。
    def has_slot(self, name, until=inf):
        times = [self.now]+[a for s,a,b in self.plant.bookings if s==name and self.now<a<until]
        return all(self.occupancy(name,t)<self.plant.capacity[name] for t in times)

    # 作用：保护同机早序炉必经的末道精炼工位，避免晚序炉占位反锁前炉。
    # 参数：self 为现场，j 为即将送往接收位的任务。
    # 返回：目的位有余量且接续可执行时为 True。
    def accepts(self, j):
        if not self.has_slot(j.dest):
            return False
        child = self.child.get(j.key)
        if not child or child.key in self.child:
            return True
        prior = child.cast_prev
        while prior:
            before = self.jobs[prior]
            if (before.source==j.dest and prior not in self.pick
                    and self.plant.capacity[j.dest]-self.occupancy(j.dest,self.now)<=1):
                return False
            prior = before.cast_prev
        return True

    # 作用：拼接指定车辆行程并验证相邻车连续间距。
    # 参数：self 为现场，car 为车索引，stops 为依次停车的坐标，load 为负载档，start 为启程时间。
    # 返回：(新增相位，结束时刻)；有冲突时返回 None。
    def route(self, car, stops, load, start):
        cur, t = self.pos[car], start
        parts = ()
        for dst in stops:
            leg = shifted(template(self.plant.rail, cur, dst, load),t)
            if leg:
                t = leg[-1].end
                parts += leg
            cur = dst
        mine = (self.plant.origins[car], self.traces[car]+parts)
        for n in (car-1,car+1):
            if 0<=n<len(self.pos):
                other = (self.plant.origins[n],self.traces[n])
                pair = (other,mine) if n<car else (mine,other)
                if min_gap(*pair,since=self.now)+EPS < self.plant.rail.gap:
                    return None
        return parts,t

    # 作用：登记已通过安全校验的运动。
    # 参数：self 为现场，car 为车号，dest 为终点，route 为轨迹与结束时刻，label 为运动类型，key 为任务号。
    # 返回：无；更新轨迹、终点和移动日志。
    def move(self, car, dest, route, label, key):
        parts,t = route
        self.traces[car] += parts
        self.pos[car] = dest
        if parts:
            self.logs.append((car,key,label,parts[0].start,t))
