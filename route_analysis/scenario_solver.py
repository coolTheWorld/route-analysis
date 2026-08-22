"""Clearance engine and solvers behind the rapid-estimate tab.

Kept apart from the clearance-headroom solver: this page has no real path, only pose
sequences generated from parameters, so a whole maneuver goes into numpy arrays and
then through shapely's vectorised calls instead of one ``PosePoint`` at a time. The
worst forward solve runs close to ten thousand evaluations; per-pose shapely would put
that an order of magnitude out of reach.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
import shapely
from shapely.geometry import LinearRing, Polygon

from route_analysis.clearance_geometry import corner_radii
from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    SOLVED_KEYS,
    Gear,
    Line,
    Offsets,
    Point,
    Primitive,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    ScenarioLayout,
    SolveMode,
    build_layout,
)
from route_analysis.turn_radius import CornerRadiusKind

FINE_ARC_STEP = 0.015
FINE_LINE_STEP = 0.06
COARSE_ARC_STEP = 0.09
COARSE_LINE_STEP = 0.18
"""Search coarse, verify fine: bisection only needs feasibility, the reading needs precision."""

FEASIBLE_SLACK = 4e-4
SCAN_SAMPLES = 10
BISECTION_STEPS = 10
TIGHTEN_STEPS = 12
GAUSS_SEIDEL_ROUNDS = 3
GUARD_ROUNDS = 3
BAND_SAMPLES = 24
MIN_PENETRATION = 1e-3


@dataclass(frozen=True, slots=True)
class Steps:
    arc: float
    line: float


FINE = Steps(FINE_ARC_STEP, FINE_LINE_STEP)
COARSE = Steps(COARSE_ARC_STEP, COARSE_LINE_STEP)


@dataclass(frozen=True, slots=True)
class PoseSamples:
    """Every pose along one maneuver, laid out as arrays so a whole batch evaluates at once."""

    x: np.ndarray
    y: np.ndarray
    ux: np.ndarray
    uy: np.ndarray
    gear_is_drive: np.ndarray
    on_arc: np.ndarray
    centre_x: np.ndarray
    centre_y: np.ndarray
    corners: np.ndarray

    def __len__(self) -> int:
        return int(self.x.size)


def _primitive_samples(
    primitive: Primitive, steps: Steps
) -> tuple[np.ndarray, ...]:
    if isinstance(primitive, Line):
        dx = primitive.end[0] - primitive.start[0]
        dy = primitive.end[1] - primitive.start[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            empty = np.empty(0)
            return empty, empty, empty, empty, empty, empty
        count = max(2, math.ceil(length / steps.line))
        t = np.linspace(0.0, 1.0, count + 1)
        x = primitive.start[0] + dx * t
        y = primitive.start[1] + dy * t
        ux = np.full_like(t, dx / length)
        uy = np.full_like(t, dy / length)
        nan = np.full_like(t, np.nan)
        return x, y, ux, uy, nan, nan
    span = primitive.end_angle - primitive.start_angle
    count = max(6, math.ceil(abs(span) / steps.arc))
    angle = primitive.start_angle + span * np.linspace(0.0, 1.0, count + 1)
    sign = 1.0 if span >= 0 else -1.0
    cos_a = np.cos(angle)
    sin_a = np.sin(angle)
    x = primitive.centre[0] + primitive.radius * cos_a
    y = primitive.centre[1] + primitive.radius * sin_a
    return (
        x,
        y,
        -sin_a * sign,
        cos_a * sign,
        np.full_like(angle, primitive.centre[0]),
        np.full_like(angle, primitive.centre[1]),
    )


def sample_poses(
    primitives: tuple[Primitive, ...], vehicle: VehicleDimensions, steps: Steps
) -> PoseSamples:
    """Expand a maneuver into pose arrays and the four body corners each pose occupies."""

    parts: list[tuple[np.ndarray, ...]] = []
    gears: list[np.ndarray] = []
    for primitive in primitives:
        sampled = _primitive_samples(primitive, steps)
        if sampled[0].size == 0:
            continue
        parts.append(sampled)
        gears.append(
            np.full(sampled[0].shape, primitive.gear is Gear.DRIVE, dtype=bool)
        )
    if not parts:
        empty = np.empty(0)
        return PoseSamples(
            empty, empty, empty, empty,
            np.empty(0, dtype=bool), np.empty(0, dtype=bool),
            empty, empty, np.empty((0, 4, 2)),
        )
    x, y, ux, uy, cx, cy = (np.concatenate(item) for item in zip(*parts, strict=True))
    gear_is_drive = np.concatenate(gears)
    lead = np.where(gear_is_drive, vehicle.center_front, vehicle.center_rear)
    trail = np.where(gear_is_drive, vehicle.center_rear, vehicle.center_front)
    half = vehicle.width / 2
    nx, ny = -uy, ux
    fx, fy = x + lead * ux, y + lead * uy
    rx, ry = x - trail * ux, y - trail * uy
    corners = np.stack(
        (
            np.stack((fx + half * nx, fy + half * ny), axis=-1),
            np.stack((fx - half * nx, fy - half * ny), axis=-1),
            np.stack((rx - half * nx, ry - half * ny), axis=-1),
            np.stack((rx + half * nx, ry + half * ny), axis=-1),
        ),
        axis=1,
    )
    return PoseSamples(
        x, y, ux, uy, gear_is_drive, ~np.isnan(cx), cx, cy, corners
    )


_REGION_CACHE: dict[tuple[Point, ...], tuple[Polygon, LinearRing, np.ndarray]] = {}
REGION_CACHE_LIMIT = 512


def _region_geometry(
    layout: ScenarioLayout,
) -> tuple[Polygon, LinearRing, np.ndarray]:
    """The region only moves with the road dimensions, yet rebuilding it per offset sample

    was costing close to half of every evaluation during the scans.
    """

    cached = _REGION_CACHE.get(layout.region)
    if cached is None:
        if len(_REGION_CACHE) >= REGION_CACHE_LIMIT:
            _REGION_CACHE.clear()
        cached = (
            Polygon(layout.region),
            LinearRing(layout.region),
            np.asarray(layout.region),
        )
        _REGION_CACHE[layout.region] = cached
    return cached


@dataclass(frozen=True, slots=True)
class Probe:
    """One evaluation: the smallest clearance and the pose that produced it."""

    clearance: float
    maneuver: str = ""
    pose_index: int = -1
    body_point: tuple[float, float] | None = None
    wall_point: tuple[float, float] | None = None
    corners: tuple[tuple[float, float], ...] = ()
    on_arc: bool = False
    gear: Gear = Gear.DRIVE
    centre: tuple[float, float] | None = None
    pose: tuple[float, float] | None = None


def _penetration(corners: np.ndarray, region: Polygon, ring: LinearRing) -> np.ndarray:
    """How deep a breaching body reaches past the wall; the caller applies the sign."""

    flat = corners.reshape(-1, 2)
    points = shapely.points(flat)
    outside = ~shapely.covers(region, points)
    depth = np.where(outside, shapely.distance(points, ring), 0.0)
    return depth.reshape(corners.shape[0], 4).max(axis=1)


def _vertex_penetration(rect: np.ndarray, region_coords: np.ndarray) -> float:
    """With all four corners inside but the body still across a notch, measure the notch

    vertex reaching into the body instead.
    """

    body = Polygon(rect)
    if not body.is_valid:
        return MIN_PENETRATION
    deepest = 0.0
    edge = LinearRing(rect)
    for vertex in region_coords:
        point = shapely.points(vertex)
        if shapely.covers(body, point):
            deepest = max(deepest, float(shapely.distance(point, edge)))
    return deepest or MIN_PENETRATION


def evaluate_layout(
    layout: ScenarioLayout,
    vehicle: VehicleDimensions,
    steps: Steps,
    *,
    detail: bool = False,
) -> Probe:
    """Smallest clearance over the whole layout: worst pose of every maneuver.

    With ``detail`` off this takes the search path: body-to-wall distance, which falls to
    zero the moment the body crosses, plus one containment test on the pose centre. That
    settles "does it clear the threshold" while skipping the per-pose polygon containment
    and penetration measurement that were costing half the search. How deep a breach goes
    is only worth computing for the one evaluation that gets reported.
    """

    if not layout.buildable:
        return Probe(clearance=-0.5 - layout.radius_shortfall)
    region, ring, region_coords = _region_geometry(layout)
    best = Probe(clearance=math.inf)
    for maneuver in layout.maneuvers:
        samples = sample_poses(maneuver.primitives, vehicle, steps)
        if not len(samples):
            continue
        polygons = shapely.polygons(samples.corners)
        values = shapely.distance(polygons, ring)
        if detail:
            covered = shapely.covers(region, polygons)
            if not covered.all():
                breached = ~covered
                depth = _penetration(samples.corners[breached], region, ring)
                for slot, pose in enumerate(np.flatnonzero(breached)):
                    if depth[slot] <= 0.0:
                        depth[slot] = _vertex_penetration(
                            samples.corners[pose], region_coords
                        )
                values[breached] = -np.where(depth > 0.0, depth, MIN_PENETRATION)
        else:
            inside = shapely.contains_xy(region, samples.x, samples.y)
            if not inside.all():
                values[~inside] = -values[~inside] - MIN_PENETRATION
        index: int = int(values.argmin())
        if float(values[index]) < best.clearance:
            best = _describe(
                float(values[index]), index, samples, maneuver.label,
                polygons[index], ring, detail=detail,
            )
    if math.isinf(best.clearance):
        return Probe(clearance=-0.5)
    return best


def _describe(
    value: float,
    index: int,
    samples: PoseSamples,
    label: str,
    body: Polygon,
    ring: LinearRing,
    *,
    detail: bool,
) -> Probe:
    body_point = wall_point = None
    if detail:
        segment = shapely.shortest_line(body, ring)
        if segment is not None:
            head, tail = segment.coords[0], segment.coords[-1]
            body_point = (float(head[0]), float(head[1]))
            wall_point = (float(tail[0]), float(tail[1]))
    on_arc = bool(samples.on_arc[index])
    return Probe(
        clearance=value,
        maneuver=label,
        pose_index=index,
        body_point=body_point,
        wall_point=wall_point,
        corners=tuple(
            (float(px), float(py)) for px, py in samples.corners[index]
        ),
        on_arc=on_arc,
        gear=Gear.DRIVE if samples.gear_is_drive[index] else Gear.REVERSE,
        centre=(
            (float(samples.centre_x[index]), float(samples.centre_y[index]))
            if on_arc
            else None
        ),
        pose=(float(samples.x[index]), float(samples.y[index])),
    )


def evaluate(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    offsets: Offsets,
    steps: Steps,
    *,
    detail: bool = False,
) -> Probe:
    return evaluate_layout(
        build_layout(inputs, dims, offsets), inputs.dimensions, steps, detail=detail
    )


@dataclass(frozen=True, slots=True)
class OffsetSpec:
    key: str
    low: float
    high: float


def offset_specs(inputs: ScenarioInputs, dims: RoadDimensions) -> tuple[OffsetSpec, ...]:
    """Which offsets are free to move under these inputs, and how far each may travel."""

    half = inputs.dimensions.width / 2

    def room(width: float) -> float:
        return max(0.0, width / 2 - half - inputs.threshold - 0.004)

    specs: list[OffsetSpec] = []
    if inputs.scenario is Scenario.UTURN:
        specs.append(OffsetSpec("yc", -1.4, max(0.4, dims.d - 0.15)))
    if not inputs.extreme:
        return tuple(specs)
    if inputs.scenario is Scenario.CORNER:
        specs.append(OffsetSpec("ea", -room(dims.wa), room(dims.wa)))
        if not inputs.bidirectional:
            specs.append(OffsetSpec("eb", -room(dims.wb), room(dims.wb)))
    elif inputs.scenario is Scenario.CROSSBACK:
        if not inputs.bidirectional:
            specs.append(OffsetSpec("ev", -room(dims.wv), room(dims.wv)))
        specs.append(OffsetSpec("eh", -room(dims.wh), room(dims.wh)))
    elif inputs.scenario is Scenario.STUBBACK:
        specs.append(OffsetSpec("a", -room(dims.wh), room(dims.wh)))
        if not inputs.bidirectional:
            specs.append(OffsetSpec("so", -room(dims.wv), room(dims.wv)))
    elif not inputs.bidirectional:
        specs.append(OffsetSpec("e1", -room(dims.w), room(dims.w)))
        specs.append(OffsetSpec("e2", -room(dims.w), room(dims.w)))
    else:
        specs.append(OffsetSpec("eo", -room(dims.w), room(dims.w)))
    return tuple(specs)


def initial_offsets(inputs: ScenarioInputs) -> Offsets:
    return Offsets(yc=0.25 if inputs.scenario is Scenario.UTURN else 0.0)


def optimise_offsets(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    steps: Steps,
    *,
    cheap: bool,
) -> tuple[Offsets, float]:
    """Coordinate descent: scan one variable at a time, shrink its bracket, repeat."""

    specs = offset_specs(inputs, dims)
    offsets = initial_offsets(inputs)
    if not specs:
        return offsets, evaluate(inputs, dims, offsets, steps).clearance
    brackets = [(spec.low, spec.high) for spec in specs]
    best = evaluate(inputs, dims, offsets, steps).clearance
    rounds = 2 if cheap else 3
    samples = 5 if cheap else 9
    for _ in range(rounds):
        for slot, spec in enumerate(specs):
            low, high = brackets[slot]
            chosen = getattr(offsets, spec.key)
            for step in range(samples + 1):
                value = low + (high - low) * step / samples
                probe = evaluate(inputs, dims, offsets.with_value(spec.key, value), steps)
                if probe.clearance > best + 1e-6:
                    best = probe.clearance
                    chosen = value
            offsets = offsets.with_value(spec.key, chosen)
            half = (high - low) * 0.28
            brackets[slot] = (
                max(spec.low, chosen - half),
                min(spec.high, chosen + half),
            )
    return offsets, best


def _feasible(
    inputs: ScenarioInputs, dims: RoadDimensions, *, thorough: bool = False
) -> bool:
    target = inputs.threshold - FEASIBLE_SLACK
    if inputs.optimises_offsets:
        return optimise_offsets(inputs, dims, COARSE, cheap=not thorough)[1] >= target
    return evaluate(inputs, dims, initial_offsets(inputs), COARSE).clearance >= target


def _minimal_feasible(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    key: str,
    low: float,
    high: float,
    *,
    thorough: bool = False,
) -> float | None:
    """Smallest feasible value this dimension can take within ``[low, high]``.

    Feasibility is not a monotone half-line. Widening a U-turn aisle also widens the
    turning circle, so the swept apex reaches further into the end head and "wider" can
    stop fitting. Plain bisection lands on the infeasible side and pins the answer to the
    search ceiling, so scan for the lowest feasible sample first and bisect only to its
    left. The current value joins the scan so the solve never steps off a feasible point.
    """

    current = getattr(dims, key)
    grid = {low + (high - low) * step / SCAN_SAMPLES for step in range(SCAN_SAMPLES + 1)}
    if low <= current <= high:
        grid.add(current)
    previous: float | None = None
    for value in sorted(grid):
        if _feasible(inputs, dims.with_value(key, value), thorough=thorough):
            if previous is None:
                return value
            lower, upper = previous, value
            for _ in range(BISECTION_STEPS):
                middle = (lower + upper) / 2
                if _feasible(inputs, dims.with_value(key, middle), thorough=thorough):
                    upper = middle
                else:
                    lower = middle
            return upper
        previous = value
    return None


@dataclass(frozen=True, slots=True)
class ForwardSolution:
    dims: RoadDimensions | None
    ceiling_clearance: float
    """Best clearance reachable with every solved dimension at its search ceiling.

    When nothing solves this is the threshold's upper bound, which lets the view say how
    far the threshold has to come down before there is an answer instead of just
    reporting failure.
    """


def solve_forward(inputs: ScenarioInputs, given: RoadDimensions) -> ForwardSolution:
    """A set of road dimensions none of which can shrink alone. The answer sits on the

    Pareto frontier and is not unique.
    """

    vehicle = inputs.dimensions
    width_low = vehicle.width + 2 * inputs.threshold + 0.005
    rear_outer = math.hypot(inputs.radius + vehicle.width / 2, vehicle.center_rear)
    swing = max(rear_outer - inputs.radius, inputs.radius)
    width_high = vehicle.width + 2 * inputs.threshold + 2 * swing + 2.4
    lane_seed = max(vehicle.width + 2 * inputs.threshold, 2 * inputs.radius - given.b) + 1
    brackets: dict[str, tuple[float, float]] = {
        "wa": (width_low, width_high),
        "wb": (width_low, width_high),
        "wv": (width_low, width_high),
        "wh": (width_low, width_high),
        "ls": (0.02, inputs.radius + vehicle.center_rear + inputs.threshold + 1.8),
        "w": (
            max(width_low, 2 * inputs.radius - given.b + 0.01),
            width_high + 2 * inputs.radius,
        ),
        "d": (0.25, inputs.radius + lane_seed + vehicle.center_rear + inputs.threshold + 3),
    }
    keys = SOLVED_KEYS[inputs.scenario]
    dims = replace(
        given,
        **{key: brackets[key][1] for key in keys},
    )
    ceiling = _report_clearance(inputs, dims)[1]
    if not _feasible(inputs, dims):
        return ForwardSolution(None, ceiling)
    for _ in range(GAUSS_SEIDEL_ROUNDS):
        for key in keys:
            low, high = brackets[key]
            solved = _minimal_feasible(
                inputs, dims, key, low, max(high, getattr(dims, key))
            )
            if solved is not None:
                dims = dims.with_value(key, solved)
    for _ in range(GUARD_ROUNDS):
        check = _report_clearance(inputs, dims)[1]
        if check >= inputs.threshold:
            break
        bump = (inputs.threshold - check) + 0.006
        dims = replace(dims, **{key: getattr(dims, key) + bump for key in keys})
    return ForwardSolution(_tighten(inputs, dims, keys, brackets), ceiling)


def _report_clearance(
    inputs: ScenarioInputs, dims: RoadDimensions
) -> tuple[Offsets, float]:
    """The evaluation that gets reported; extreme conditions and U-turns optimise first."""

    if inputs.optimises_offsets:
        return optimise_offsets(inputs, dims, FINE, cheap=False)
    offsets = initial_offsets(inputs)
    return offsets, evaluate(inputs, dims, offsets, FINE).clearance


def _clamp(offsets: Offsets, specs: tuple[OffsetSpec, ...]) -> Offsets:
    for spec in specs:
        value = getattr(offsets, spec.key)
        offsets = offsets.with_value(spec.key, min(max(value, spec.low), spec.high))
    return offsets


def _tighten(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    keys: tuple[str, ...],
    brackets: dict[str, tuple[float, float]],
) -> RoadDimensions:
    """Squeeze out the slack the coarse search left behind.

    Searching coarse with a cheap offset optimiser understates the clearance actually
    reachable, so the dimensions come out large: measured at more than double the
    threshold in places. Freeze the offsets the reporting pass would choose, then bisect
    each dimension down against a single fine evaluation. Freezing keeps each step at one
    evaluation rather than dozens, which makes the whole pass nearly free. Re-run the full
    report afterwards, and if a narrowed offset bound dropped it under the threshold, fall
    back to the untightened dimensions: better conservative than an answer that will not
    fit.
    """

    offsets, before = _report_clearance(inputs, dims)
    if before < inputs.threshold:
        return dims
    original = dims
    for key in keys:
        low = brackets[key][0]
        current = getattr(dims, key)
        if current - low < 1e-3:
            continue
        frozen = _clamp(offsets, offset_specs(inputs, dims))
        floor = evaluate(inputs, dims.with_value(key, low), frozen, FINE)
        if floor.clearance >= inputs.threshold:
            dims = dims.with_value(key, low)
            continue
        lower, upper = low, current
        for _ in range(TIGHTEN_STEPS):
            middle = (lower + upper) / 2
            candidate = dims.with_value(key, middle)
            frozen = _clamp(offsets, offset_specs(inputs, candidate))
            if evaluate(inputs, candidate, frozen, FINE).clearance >= inputs.threshold:
                upper = middle
            else:
                lower = middle
        dims = dims.with_value(key, upper)
    if _report_clearance(inputs, dims)[1] < inputs.threshold:
        return original
    return dims


@dataclass(frozen=True, slots=True)
class OffsetBand:
    key: str
    low: float | None
    high: float | None

    @property
    def empty(self) -> bool:
        return self.low is None or self.high is None


def feasible_bands(
    inputs: ScenarioInputs, dims: RoadDimensions, offsets: Offsets
) -> tuple[OffsetBand, ...]:
    """How far each leg alone may move from the optimum and still clear the threshold.

    The scan has to match the reporting precision and has to include the chosen offset,
    or the optimum lands outside its own feasible band: coarse sampling reads a shade
    tighter and the grid steps straight over the chosen value, leaving two rows in the
    result column that contradict each other.
    """

    bands: list[OffsetBand] = []
    for spec in offset_specs(inputs, dims):
        if spec.key == "yc":
            continue
        chosen = getattr(offsets, spec.key)
        grid = {
            spec.low + (spec.high - spec.low) * step / BAND_SAMPLES
            for step in range(BAND_SAMPLES + 1)
        }
        if spec.low <= chosen <= spec.high:
            grid.add(chosen)
        low = high = None
        for value in sorted(grid):
            probe = evaluate(inputs, dims, offsets.with_value(spec.key, value), FINE)
            if probe.clearance >= inputs.threshold - FEASIBLE_SLACK:
                if low is None:
                    low = value
                high = value
        bands.append(OffsetBand(spec.key, low, high))
    return tuple(bands)


def _status(clearance: float, threshold: float) -> ClearanceStatus:
    """Same rule as the overrun analysis in ``analysis.py``, not a second one."""

    if clearance < 0:
        return ClearanceStatus.OUTSIDE
    if clearance < threshold:
        return ClearanceStatus.WARNING
    return ClearanceStatus.SAFE


CORNER_SIDE_NAMES: dict[tuple[bool, bool], str] = {
    (True, True): "前外角",
    (True, False): "前内角",
    (False, True): "后外角",
    (False, False): "后内角",
}


def describe_bottleneck(probe: Probe) -> str:
    """Put the tightest pose into words: which corner, which leg of which maneuver."""

    if probe.pose_index < 0 or probe.body_point is None:
        return "—"
    nearest = min(
        range(len(probe.corners)),
        key=lambda slot: math.dist(probe.corners[slot], probe.body_point or (0.0, 0.0)),
    )
    leads = nearest in (0, 1)
    physically_front = leads == (probe.gear is Gear.DRIVE)
    if probe.on_arc and probe.centre is not None and probe.pose is not None:
        outer = math.dist(probe.body_point, probe.centre) > math.dist(
            probe.pose, probe.centre
        )
        corner = CORNER_SIDE_NAMES[(physically_front, outer)]
        segment = f"{probe.gear.value}档转弯段"
    else:
        corner = "前部/侧边" if physically_front else "后部/侧边"
        segment = f"{probe.gear.value}档直行段"
    return (
        f"最紧点为{corner}扫掠（{probe.maneuver} · {segment}）。"
        f"最小净距 {probe.clearance:.2f} m，出现在图中红点处。"
    )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Everything the result column shows, arranged by row so an export can reuse it."""

    inputs: ScenarioInputs
    dims: RoadDimensions
    offsets: Offsets
    layout: ScenarioLayout
    probe: Probe
    status: ClearanceStatus
    min_clearance: float
    corner_radii: dict[CornerRadiusKind, float]
    bottleneck: str
    turn_radius: float | None = None
    radius_shortfall: float = 0.0
    required_lane_width: float | None = None
    centred_clearance: float | None = None
    bands: tuple[OffsetBand, ...] = ()
    infeasible: bool = False
    threshold_ceiling: float | None = None

    @property
    def solved(self) -> bool:
        return self.inputs.mode is SolveMode.FORWARD and not self.infeasible

    @property
    def margin(self) -> float:
        """Headroom against the threshold: positive to spare, negative short."""

        return self.min_clearance - self.inputs.threshold


def _centred_clearance(inputs: ScenarioInputs, dims: RoadDimensions) -> float:
    """The comparison row: what is left if the truck stays on the centreline after all.

    It sits beside the optimised reading, so a breach has to report its real negative
    depth. That means the detail path, not the search path, which reads any crossing as
    a flat zero.
    """

    offsets = initial_offsets(inputs)
    specs = [spec for spec in offset_specs(inputs, dims) if spec.key == "yc"]
    if inputs.scenario is not Scenario.UTURN or not specs:
        return evaluate(inputs, dims, offsets, FINE, detail=True).clearance
    spec = specs[0]
    best = -math.inf
    for step in range(11):
        value = spec.low + (spec.high - spec.low) * step / 10
        candidate = offsets.with_value("yc", value)
        best = max(best, evaluate(inputs, dims, candidate, FINE, detail=True).clearance)
    return best


def solve_scenario(inputs: ScenarioInputs, given: RoadDimensions) -> ScenarioResult:
    """One full estimate: solve for the limiting dimensions, or check the ones given."""

    radii = corner_radii(inputs.dimensions, inputs.radius)
    ceiling: float | None = None
    if inputs.mode is SolveMode.FORWARD:
        solution = solve_forward(inputs, given)
        if solution.dims is None:
            layout = build_layout(inputs, given, initial_offsets(inputs))
            return ScenarioResult(
                inputs=inputs,
                dims=given,
                offsets=initial_offsets(inputs),
                layout=layout,
                probe=Probe(clearance=solution.ceiling_clearance),
                status=ClearanceStatus.UNAVAILABLE,
                min_clearance=solution.ceiling_clearance,
                corner_radii=radii,
                bottleneck="在给定车辆参数下找不到可行的道路极限尺寸。",
                infeasible=True,
                threshold_ceiling=max(0.0, solution.ceiling_clearance),
            )
        dims = solution.dims
        ceiling = solution.ceiling_clearance
    else:
        dims = given

    offsets, clearance = _report_clearance(inputs, dims)
    layout = build_layout(inputs, dims, offsets)
    if not layout.buildable:
        return ScenarioResult(
            inputs=inputs,
            dims=dims,
            offsets=offsets,
            layout=layout,
            probe=Probe(clearance=clearance),
            status=ClearanceStatus.OUTSIDE,
            min_clearance=clearance,
            corner_radii=radii,
            bottleneck="巷道宽与隔墙宽装不下最小转弯半径，这个布局画不出来。",
            turn_radius=layout.turn_radius,
            radius_shortfall=layout.radius_shortfall,
            required_lane_width=2 * inputs.radius - dims.b,
            infeasible=True,
        )

    probe = evaluate_layout(layout, inputs.dimensions, FINE, detail=True)
    bands: tuple[OffsetBand, ...] = ()
    centred: float | None = None
    if inputs.mode is SolveMode.CHECK and inputs.extreme:
        centred = _centred_clearance(inputs, dims)
        bands = feasible_bands(inputs, dims, offsets)
    return ScenarioResult(
        inputs=inputs,
        dims=dims,
        offsets=offsets,
        layout=layout,
        probe=probe,
        status=_status(probe.clearance, inputs.threshold),
        min_clearance=probe.clearance,
        corner_radii=radii,
        bottleneck=describe_bottleneck(probe),
        turn_radius=layout.turn_radius,
        centred_clearance=centred,
        bands=bands,
        threshold_ceiling=ceiling,
    )


@dataclass(frozen=True, slots=True)
class ManeuverTrace:
    """One maneuver ready to draw: body corners per pose, gear, and which poses breach."""

    label: str
    corners: np.ndarray
    gear_is_drive: np.ndarray
    breached: np.ndarray
    x: np.ndarray
    y: np.ndarray


def trace_maneuvers(
    layout: ScenarioLayout, vehicle: VehicleDimensions, steps: Steps = FINE
) -> tuple[ManeuverTrace, ...]:
    """Expand a layout into drawable pose traces so the painter never touches shapely."""

    if not layout.buildable:
        return ()
    region, _, _ = _region_geometry(layout)
    traces = []
    for maneuver in layout.maneuvers:
        samples = sample_poses(maneuver.primitives, vehicle, steps)
        if not len(samples):
            continue
        covered = shapely.covers(region, shapely.polygons(samples.corners))
        traces.append(
            ManeuverTrace(
                label=maneuver.label,
                corners=samples.corners,
                gear_is_drive=samples.gear_is_drive,
                breached=~covered,
                x=samples.x,
                y=samples.y,
            )
        )
    return tuple(traces)
