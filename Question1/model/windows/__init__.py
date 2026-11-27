"""计划窗口与工艺关系。所有时间以整数拍表示，区间左闭右开。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    src: int
    dst: int
    upstream: int
    downstream: int
    cars: tuple[int, ...]  # 已按车型、载重和作业资格筛选的车辆编号，从 0 开始
    alpha: int = 0
    beta: int = 0
    latest_pick: int | None = None
    parents: tuple[tuple[str, int], ...] = ()  # (前置任务, 最小处理时间)
    source: str | None = None  # 从 upstream 到起吊占位
    receiver: str | None = None  # 从落包到 downstream 占位
    release_lag: int = 0  # 起吊后源位继续占用的拍数

    return_ferry: int | None = None  # 空包回程落渡车时填写目标渡车编号；dst 为该车位坐标

    # 作用：计算可起吊时间。输入：self 为任务。返回：就绪时刻。
    @property
    def ready(self):
        return self.upstream + self.alpha

    # 作用：计算最迟落包时间。输入：self 为任务。返回：落包期限。
    @property
    def due(self):
        return self.downstream - self.beta


# 作用：检查任务标识、工艺边和时间字段。输入：tasks 为同批任务。返回：无，非法时抛出 ValueError。
def validate_tasks(tasks):
    names = {task.name for task in tasks}
    if len(names) != len(tasks):
        raise ValueError("任务编号重复")
    pending = {}
    for task in tasks:
        if task.return_ferry is not None and (type(task.return_ferry) is not int or task.return_ferry not in (1, 2, 3)):
            raise ValueError(f"{task.name} 空包回程仅可落 1–3 号渡车")
        times = [task.upstream, task.downstream, task.alpha, task.beta, task.release_lag]
        if task.latest_pick is not None:
            times.append(task.latest_pick)
        if any(type(t) is not int or t < 0 for t in times):
            raise ValueError(f"{task.name} 时间必须为非负整数拍")
        if task.ready > task.due:
            raise ValueError(f"{task.name} 就绪（注入）时间晚于交付期限")
        pending[task.name] = set()
        for parent, lag in task.parents:
            if parent not in names or type(lag) is not int or lag < 0:
                raise ValueError(f"{task.name} 的工艺前置关系无效")
            pending[task.name].add(parent)
    done = set()
    while len(done) < len(tasks):
        new = {name for name, deps in pending.items() if name not in done and deps <= done}
        if not new:
            raise ValueError("工艺先后关系成环")
        done.update(new)


# 作用：把固定生产路线串成同炉任务链。输入：legs 为按工艺顺序拆好的各段任务，lags 为相邻两段间必要处理拍数。返回：加入工艺前置边的任务元组。
def chain(legs, lags):
    from dataclasses import replace
    if len(lags) != max(0, len(legs)-1):
        raise ValueError("每对相邻工序须提供一个处理时长")
    result = list(legs[:1])
    for parent, child, lag in zip(legs, legs[1:], lags):
        result.append(replace(child, parents=child.parents+((parent.name,lag),)))
    validate_tasks(result)
    return tuple(result)


@dataclass(frozen=True)
class Cast:
    machine: str
    sequence: str
    task: str
    start: int
    finish: int
    changeover: int = 0  # 本炉开浇前的计划换中包时长


# 作用：核对固定浇次并生成处理位预约。输入：casts 为每台机按生产顺序给出的炉次，tasks 为吊运任务。返回：(连铸处理位,占用开始,占用结束)序列，处理位应与接收位分别命名。
def cast_bookings(casts, tasks):
    jobs = {t.name:t for t in tasks}
    previous = {}
    rows = []
    seen = set()
    closed = set()
    for c in casts:
        if c.task in seen or c.task not in jobs or jobs[c.task].downstream != c.start:
            raise ValueError("连铸备包任务重复、缺失或开浇时刻不匹配")
        if any(type(v) is not int for v in (c.start,c.finish,c.changeover)) or not (
                0 <= c.changeover <= c.start < c.finish):
            raise ValueError("浇铸时段或换中包时长无效")
        seen.add(c.task)
        last = previous.get(c.machine)
        begin = c.start-c.changeover
        if last:
            if last.finish > begin or (last.sequence == c.sequence and last.finish != begin):
                raise ValueError("连铸时段冲突或同浇次断浇")
            if last.sequence != c.sequence:
                closed.add((last.machine,last.sequence))
        if (c.machine,c.sequence) in closed:
            raise ValueError("浇次顺序不连续")
        if c.changeover:
            rows.append((c.machine,begin,c.start))
        rows.append((c.machine,c.start,c.finish))
        previous[c.machine] = c
    return tuple(rows)
