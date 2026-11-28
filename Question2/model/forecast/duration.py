"""以整炉为抽样块的经验工时模型。实际吊起不进入工时样本。"""
from collections import defaultdict
from math import ceil
from random import Random
from statistics import fmean
from functools import lru_cache


# 作用：计算离散经验分位数，取累计概率首次达到阈值的观测。
# 参数：values 为等权观测（可重复），q 为 0 至 1 的分位水平。
# 返回：样本中对应的数值。
def quantile(values, q):
    if not values or not 0 <= q <= 1:
        raise ValueError("分位数参数或样本无效")
    a = sorted(values)
    return a[max(0, ceil(q*len(a))-1)]


class Forecast:
    # 作用：构造每个目标炉次的联合工时样本。
    # 参数：jobs 为目标任务，blocks 为历史整炉字典(kind -> (设备,实际/计划比))，minimum 为设备细分最小样本量。
    # 返回：无；实例保留按炉排列的任务和以秒计的工时向量。
    def __init__(self, jobs, blocks, minimum=5):
        self.groups = defaultdict(list)
        self.pool = {}
        self.index = {}
        for j in jobs:
            self.index[j.key] = len(self.groups[j.heat])
            self.groups[j.heat].append(j)
        for heat, group in self.groups.items():
            usable = [b for b in blocks if all(j.kind in b and b[j.kind][1]>0 for j in group)]
            exact = [b for b in usable if all(b[j.kind][0]==j.source for j in group)]
            rows = exact if len(exact)>=minimum else usable
            if not rows:
                raise ValueError(f"{heat} 缺少覆盖工艺路线的联合工时样本")
            centers = [fmean(b[j.kind][1] for b in rows) for j in group]
            self.pool[heat] = tuple(tuple(j.mean*b[j.kind][1]/m for j,m in zip(group,centers))
                                    for b in rows)

    # 作用：根据同炉已结束工时、尚在处理的经过时间更新整炉样本。
    # 参数：heat 为炉号，known 为 (段索引，已观测完整工时) 元组，running 为 (段索引，已经过工时) 元组。
    # 返回：仍可能的整炉工时向量；超出经验支持时按历史平均剩余寿命外推。
    @lru_cache(maxsize=2000)
    def conditioned(self, heat, known, running):
        rows = self.pool[heat]
        for k, elapsed in running:
            surviving = tuple(r for r in rows if r[k] > elapsed)
            if surviving:
                rows = surviving
            else:
                # 全部删失时显式尾部约定：最慢十分位的超额均值，至少一秒。
                threshold = quantile([r[k] for r in rows], .9)
                tail = [r for r in rows if r[k]>=threshold]
                extra = max(1., self.groups[heat][k].mean*.1, fmean(r[k]-threshold for r in tail))
                rows = tuple(r[:k]+(elapsed+extra,)+r[k+1:] for r in tail)
        if known:
            # 用历史中最相近的已完成工时条件化；保留并列供体。
            scores = [sum(abs(r[k]-v)/max(1.,self.groups[heat][k].mean) for k,v in known)
                      for r in rows]
            best = min(scores)
            rows = tuple(r for r,s in zip(rows,scores) if s<=best+1e-7)
        return rows

    # 作用：只从现场观测构造当前条件样本。
    # 参数：heat 为炉号，now 为决策时间，starts/ends 为已观察到的开工/完工时间字典。
    # 返回：工时向量元组，向量顺序与该炉工艺段相同。
    def belief(self, heat, now, starts, ends):
        known, running = [], []
        for i,j in enumerate(self.groups[heat]):
            if j.key in ends:
                known.append((i, ends[j.key]-starts[j.key]))
            elif j.key in starts:
                running.append((i, now-starts[j.key]))
        return self.conditioned(heat, tuple(known), tuple(running))

    # 作用：从相同观测生成配对回放的外生样本，每炉联合抽一个向量。
    # 参数：seed 为复现种子，now 为时刻，starts/ends 为已知开工/完工表。
    # 返回：任务号到场景工时秒数的字典。
    def draw(self, seed, now=0., starts=None, ends=None):
        rng = Random(seed)
        if starts is None:
            starts = {j.key:-j.initial_elapsed for group in self.groups.values() for j in group
                      if j.initial_elapsed}
        world = {}
        for heat, group in self.groups.items():
            row = rng.choice(self.belief(heat, now, starts or {}, ends or {}))
            world.update((j.key,d) for j,d in zip(group,row))
        return world
