from dataclasses import dataclass


@dataclass(frozen=True)
class Crane:
    name: str
    position: float
    capacity: float
    empty_speed: float = 2
    loaded_speed: float = 1
    acceleration: float = 1


@dataclass(frozen=True)
class Task:
    name: str
    original: int
    ready: int
    due: int
    crane: str
    origin: float
    destination: float
    service: int
    eligible: tuple
    parents: tuple = ()
    station: str = ''
    dwell: int = 0
    weight: float = 1
    locked: bool = False


@dataclass(frozen=True)
class Case:
    tasks: tuple
    cranes: tuple
    now: int = 0
    horizon: int = 50
    gap: float = 1
    # 工位不可用区间 (工位名, 开始, 结束)，容量为一
    outages: tuple = ()


@dataclass(frozen=True)
class Trip:
    task: str
    crane: str
    start: int
    end: int
    motion: tuple
    moving: float
