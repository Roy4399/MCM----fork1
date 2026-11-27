"""一维共轨网格。每拍匀速运行一格，或停在合法停车点。"""
from dataclasses import dataclass
from collections import Counter


@dataclass(frozen=True)
class Track:
    initial: tuple[int, ...]  # 按从左到右顺序的车位
    edges: tuple[frozenset[tuple[int, int]], ...]  # 每车合法有向边
    parking: frozenset[int]
    gaps: tuple[int, ...]  # 相邻车辆的最小间距


# 作用：校验同一折点的邻车间距。输入：positions 为有序车位，gaps 为间距要求。返回：是否全部合规。
def separated(positions, gaps):
    return all(b - a >= gap for a, b, gap in zip(positions, positions[1:], gaps))


# 作用：回放各拍间线性轨迹。输入：frames 为等间隔同步车位，gaps 为安全间距。返回：每段是否合规。
def safe_trace(frames, gaps):
    return all(len(frame) == len(gaps) + 1 and separated(frame, gaps) for frame in frames)


# 作用：检查资源容量。输入：bookings 为(资源,开始,结束)预约，caps 为容量字典。返回：能否同时容纳。
def fits(bookings, caps):
    changes = {key: Counter() for key in caps}
    for key, start, end in bookings:
        if key is None:
            continue
        if key not in caps or end < start:
            return False
        changes[key][start] += 1
        changes[key][end] -= 1
    for key, events in changes.items():
        used = 0
        for time in sorted(events):
            used += events[time]
            if used > caps[key]:
                return False
    return True
