"""Event-based common-rail reservations for a uniform-speed track.

Coordinates are travel seconds. Subtracting cumulative clearances from crane
positions turns the separation constraint into an ordinary order constraint.
"""

from bisect import bisect_left, insort
from collections import defaultdict
from dataclasses import dataclass
from itertools import accumulate, pairwise
from math import ceil


@dataclass(frozen=True)
class Job:
    name: str
    row: int
    heat: str
    kind: str
    origin: str
    destinations: tuple[str, ...]
    ready: int
    due: int
    cars: tuple[int, ...]
    before: int = 0
    source: str | None = None
    receiver: str | None = None
    tonnes: float | None = None


@dataclass(frozen=True)
class Lift:
    job: str
    car: int
    origin: str
    destination: str
    pickup: int
    arrival: int


# 作用：按场地吨位表筛选行车。输入：site 为场地参数，tonnes 为包重或 None。返回：可用行车的内部编号。
def eligible_cranes(site, tonnes):
    minimum = 450 if tonnes is None else tonnes
    return tuple(i for i, limit in enumerate(site["limits"]) if limit >= minimum)


# 作用：计算水平移动及原位交接时长。输入：场地、任务和目的地。返回：(移动秒数, 原位作业秒数)。
def lift_times(site, job, destination):
    distance = abs(site["points"][job.origin] - site["points"][destination])
    service = (site.get("colocated_ferry_transfer_seconds", 0)
               if not distance and job.kind == "empty_to_bof" else 0)
    if distance + service <= 0:
        raise ValueError(f"overlapping modeled locations for {job.name}")
    return distance, service


class Reservations:
    # 作用：初始化行车预约状态。输入：self 为预约对象，site 为场地配置。返回：无。
    def __init__(self, site):
        self.site = site
        self.gaps = tuple(accumulate(site["gaps"], initial=0))
        self.anchors = [[(0, x)] for x in site["initial"]]
        self.loaded = [[] for _ in self.anchors]
        self.resources = defaultdict(list)
        self.lifts = {}

    # 作用：找出工位已满的时间区间。输入：self 为预约状态，resource 为工位名。返回：容量饱和区间。
    def saturated(self, resource):
        items = self.resources[resource]
        capacity = self.site.get("buffer_capacities", {}).get(resource, 1)
        if capacity == 1:
            return items
        concurrent, begin, blocked = 0, None, []
        events = sorted((t, add) for a,b,_ in items for t,add in ((a, 1), (b, -1)))
        for t, add in events:
            concurrent += add
            if add > 0 and concurrent == capacity:
                begin = t
            if add < 0 and concurrent == capacity - 1:
                blocked.append((begin, t, "capacity"))
        return blocked

    # 作用：检查行车位置的可达性。输入：self 为预约，car 为车号，x 为轨道坐标。返回：是否可达。
    def reachable(self, car, x):
        left, right = self.site["bounds"]
        return left + self.gaps[car] <= x <= right - self.gaps[-1] + self.gaps[car]

    # 作用：计算避开已有轨迹锚点所需的时刻。输入：self 为预约，car 为车号，time、position 为新锚点时空坐标，pickup 为起吊时刻。返回：最早可继续试探的起吊时刻。
    def conflict_end(self, car, time, position, pickup):
        """Return the first allowable boundary after each violated anchor."""
        left, right = self.site["bounds"]
        span = right - left
        y = position - self.gaps[car]
        offset = time - pickup
        skip = pickup
        for other, anchors in enumerate(self.anchors):
            start = bisect_left(anchors, (time - span,))
            stop = bisect_left(anchors, (time + span,))
            for at, x in anchors[start:stop]:
                delta = y - (x - self.gaps[other])
                distance = abs(delta) if car == other else delta if car < other else -delta
                if distance > abs(at - time):
                    skip = max(skip, at + distance - offset)
        return skip

    # 作用：登记工位占用。输入：self 为预约，resource 为工位，start、end 为占用区间，name 为任务名。返回：无。
    def reserve(self, resource, start, end, name):
        if resource is not None and start < end:
            insort(self.resources[resource], (start, end, name))

    # 作用：枚举最早可行的吊运插入。输入：self 为预约，job 为任务，earliest、latest 限定起吊时刻，destination_hold、hold_from、departure_hold 为工位占用参数。返回：可行吊运记录迭代器。
    def proposals(self, job, earliest=None, latest=None, destination_hold=0, hold_from=None, departure_hold=0):
        """Enumerate earliest feasible insertion for each eligible vehicle and destination."""
        site = self.site
        src = site["points"][job.origin]
        original_ready = job.ready if hold_from is None else hold_from
        start = max(job.ready, earliest or 0, original_ready)
        stop = min(job.due, latest if latest is not None else job.due)
        for begin, end, _ in self.saturated(job.source):
            if end > original_ready:
                # A pickup at begin shares no resource interval with the existing booking.
                stop = min(stop, max(original_ready, begin))
                break
        for destination in job.destinations:
            dst = site["points"][destination]
            distance, service = lift_times(site, job, destination)
            duration = distance + service
            receiver = job.receiver if job.receiver else destination if destination_hold else None
            pickup_floor = start
            if job.receiver:
                # Receiving buffer stays occupied until the scheduled process starts.
                for begin, end, _ in self.saturated(receiver):
                    if begin >= job.due:
                        break
                    pickup_floor = max(pickup_floor, min(end, job.due) - duration)
            for car in job.cars:
                if not (self.reachable(car, src) and self.reachable(car, dst)):
                    continue
                pickup = pickup_floor
                latest_pick = min(stop, job.due - duration)
                car_jobs = self.loaded[car]
                occupied = self.saturated(receiver) if destination_hold else ()
                while pickup <= latest_pick:
                    arrival = pickup + duration
                    skip = max(pickup, self.conflict_end(car, pickup, src, pickup),
                               self.conflict_end(car, arrival, dst, pickup))
                    if service and skip == pickup:
                        # A later neighbour anchor can conflict inside an otherwise valid dwell.
                        y = src - self.gaps[car]
                        for other, anchors in enumerate(self.anchors):
                            if other == car:
                                continue  # Same-vehicle overlap is checked by car_jobs below.
                            lo = bisect_left(anchors, (pickup + 1,))
                            hi = bisect_left(anchors, (arrival,))
                            for at, x in anchors[lo:hi]:
                                delta = y - (x - self.gaps[other])
                                separation = delta if car < other else -delta
                                if separation > 0:
                                    skip = max(skip, at + separation)
                    index = max(0, bisect_left(car_jobs, (pickup,)) - 1)
                    for a, z, _ in car_jobs[index:]:
                        if a >= arrival:
                            break
                        if z > pickup:
                            skip = max(skip, z)
                    index = max(0, bisect_left(occupied, (arrival,)) - 1)
                    for a, z, _ in occupied[index:]:
                        if a >= arrival + destination_hold:
                            break
                        if z > arrival:
                            skip = max(skip, z - duration)
                    if departure_hold:
                        used = self.saturated(job.origin)
                        index = max(0, bisect_left(used, (pickup - departure_hold,)) - 1)
                        for a, z, _ in used[index:]:
                            if a >= pickup:
                                break
                            if z > pickup - departure_hold:
                                skip = max(skip, z + departure_hold)
                    if skip == pickup:
                        yield Lift(job.name, car, job.origin, destination, pickup, arrival)
                        break
                    pickup = skip

    # 作用：生成任务轨迹锚点。输入：self 为预约，lift 为吊运记录。返回：起落坐标或原位交接期间的固定锚点。
    def lift_anchors(self, lift):
        """Return rail anchors for a lift; lock every modeled second of a stationary transfer."""
        points = self.site["points"]
        source = points[lift.origin]
        dest = points[lift.destination]
        if source == dest:
            return ((t, source) for t in range(lift.pickup, lift.arrival + 1))
        return ((lift.pickup, source), (lift.arrival, dest))

    # 作用：提交吊运及占用。输入：self 为预约，job、lift 为任务及安排，destination_hold、hold_from、departure_hold 为工位占用参数。返回：无。
    def add(self, job, lift, destination_hold=0, hold_from=None, departure_hold=0):
        if job.name in self.lifts:
            raise ValueError(f"duplicate lift {job.name}")
        for anchor in self.lift_anchors(lift):
            insort(self.anchors[lift.car], anchor)
        insort(self.loaded[lift.car], (lift.pickup, lift.arrival, job.name))
        self.reserve(job.source, job.ready if hold_from is None else hold_from, lift.pickup, job.name)
        self.reserve(job.receiver, lift.arrival, job.due, job.name)
        self.reserve(lift.destination if destination_hold else None,
                     lift.arrival, lift.arrival + destination_hold, job.name)
        self.reserve(lift.origin if departure_hold else None,
                     lift.pickup - departure_hold, lift.pickup, job.name)
        self.lifts[job.name] = lift

    # 作用：移除已提交的任务和关联占用。输入：self 为预约，job 为任务。返回：无。
    def remove(self, job):
        lift = self.lifts.pop(job.name)
        for anchor in self.lift_anchors(lift):
            self.anchors[lift.car].remove(anchor)
        self.loaded[lift.car].remove((lift.pickup, lift.arrival, job.name))
        for name in {job.source, job.receiver, lift.origin, lift.destination}:
            if name is not None:
                records = self.resources[name]
                records[:] = [record for record in records if record[2] != job.name]

    # 作用：根据预约重建五车轨迹。输入：self 为预约状态。返回：各车时空折点。
    def trace(self):
        """Construct feasible positions, waiting first and then moving at speed one."""
        clocks = sorted({t for anchors in self.anchors for t, _ in anchors})
        gaps = self.gaps
        left, right = self.site["bounds"]
        cursors = [0] * len(self.anchors)
        positions = [x - g for x, g in zip(self.site["initial"], gaps)]
        previous = 0
        traces = [[(0, x)] for x in self.site["initial"]]
        for at in clocks[1:]:
            lows, highs = [], []
            for i, anchors in enumerate(self.anchors):
                cursor = cursors[i]
                while cursor + 1 < len(anchors) and anchors[cursor + 1][0] <= at:
                    cursor += 1
                cursors[i] = cursor
                nearby = anchors[cursor:cursor + 2]
                lows.append(max(x - gaps[i] - abs(t - at) for t, x in nearby))
                highs.append(min(x - gaps[i] + abs(t - at) for t, x in nearby))
            floor = tuple(accumulate((max(left, lo) for lo in lows), max))
            ceiling = tuple(reversed(tuple(accumulate(
                (min(right - gaps[-1], hi) for hi in reversed(highs)), min))))
            for i, old in enumerate(positions):
                new = min(max(old, floor[i]), ceiling[i])
                if floor[i] > ceiling[i] or abs(new - old) > at - previous:
                    raise ValueError(f"inconsistent point reservations at {at}, crane {i}")
                if new != old:
                    traces[i].append((at - abs(new - old), old + gaps[i]))
                    traces[i].append((at, new + gaps[i]))
                positions[i] = new
            previous = at
        for trace in traces:
            if trace[-1][0] != previous:
                trace.append((previous, trace[-1][1]))
        return tuple(tuple(dict.fromkeys(tr)) for tr in traces)


# 作用：回放任务、轨迹和资源占用并复算指标。输入：site 为场地，jobs、lifts 为任务及安排，traces 为轨迹，bookings 为资源占用。返回：审计指标字典。
def verify(site, jobs, lifts, traces, bookings):
    """Audit material coverage and regenerate the key constraints from the result."""
    by_name = {j.name: j for j in jobs}
    missing = set(by_name) - set(lifts)
    if len(by_name) != len(jobs) or set(lifts) - by_name.keys():
        raise ValueError("invalid task identifiers")
    empty_distance, loaded_distance, stationary_service, waits = 0, 0, 0, []
    by_car = defaultdict(list)
    for name, lift in lifts.items():
        j = by_name[name]
        if (lift.job != name or lift.origin != j.origin or lift.destination not in j.destinations
                or lift.car not in j.cars or not (j.ready <= lift.pickup < lift.arrival <= j.due)
                or lift.car not in eligible_cranes(site, j.tonnes)):
            raise ValueError(f"window, endpoint or load qualification failed for {name}")
        distance, service = lift_times(site, j, lift.destination)
        if distance + service != lift.arrival - lift.pickup:
            raise ValueError(f"wrong travel time for {name}")
        loaded_distance += distance
        stationary_service += service
        waits.append(lift.pickup - j.ready)
        by_car[lift.car].append(lift)
    occupancy_peaks = {}
    for resource, intervals in bookings.items():
        events = sorted((t, change) for a, b, _ in intervals for t, change in ((a, 1), (b, -1)))
        peak = max(accumulate(change for _, change in events), default=0)
        if peak > site.get("buffer_capacities", {}).get(resource, 1):
            raise ValueError(f"buffer capacity violated at {resource}")
        occupancy_peaks[resource] = peak

    left, right = site["bounds"]
    for i, segments in enumerate(traces):
        if segments[0] != (0, site["initial"][i]):
            raise ValueError(f"invalid initial crane position {i}")
        if not all(left <= x <= right for _, x in segments):
            raise ValueError(f"crane {i} is outside the rail")
        for (a, x), (b, y) in pairwise(segments):
            if b <= a or (y != x and abs(y - x) != b - a):
                raise ValueError(f"invalid speed at {a}, crane {i}")
            empty_distance += abs(y - x)
        sorted_lifts = sorted(by_car[i], key=lambda item: item.pickup)
        if any(a.arrival > b.pickup for a, b in pairwise(sorted_lifts)):
            raise ValueError(f"double-booked crane {i}")
        for lift in sorted_lifts:
            x1 = position_at(segments, lift.pickup)
            x2 = position_at(segments, lift.arrival)
            if (x1, x2) != (site["points"][lift.origin], site["points"][lift.destination]):
                raise ValueError(f"lift endpoints do not match trajectory for {lift.job}")
            if x1 == x2:
                start = bisect_left(segments, (lift.pickup,))
                stop = bisect_left(segments, (lift.arrival,))
                if any(x != x1 for _, x in segments[start:stop]):
                    raise ValueError(f"crane moved during stationary transfer for {lift.job}")

    minimum_gaps = []
    for i, (one, two) in enumerate(pairwise(traces)):
        times = {t for t, _ in one} | {t for t, _ in two}
        gap = min(position_at(two, at) - position_at(one, at) for at in times)
        if gap < site["gaps"][i]:
            raise ValueError(f"cranes {i} and {i + 1} have inadequate clearance")
        minimum_gaps.append(gap)
    result = {"assigned": len(lifts), "missing": len(missing), "wait_seconds": sum(waits),
            "wait_p95_seconds": sorted(waits)[ceil(.95 * len(waits)) - 1] if waits else None,
            "loaded_motion_seconds": loaded_distance, "empty_motion_seconds": empty_distance - loaded_distance,
            "min_adjacent_gaps": minimum_gaps, "max_occupancy_by_resource": occupancy_peaks}
    if site.get("colocated_ferry_transfer_seconds"):
        result["stationary_service_seconds"] = stationary_service
    return result


# 作用：计算指定时刻的车位。输入：segments 为轨迹折点，at 为查询时刻。返回：线性插值得到的轨道坐标。
def position_at(segments, at):
    i = bisect_left(segments, (at,))
    if i == len(segments):
        return segments[-1][1]
    b, y = segments[i]
    if b == at:
        return y
    a, x = segments[i - 1]
    return x if x == y else x + (at - a) * (1 if y > x else -1)
