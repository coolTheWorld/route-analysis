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
from itertools import product

import numpy as np
import shapely
from shapely.geometry import LinearRing, Polygon

from route_analysis import scenario_geometry
from route_analysis.clearance_geometry import corner_radii
from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    CENTRED,
    NO_PINS,
    SOLVED_KEYS,
    Condition,
    Gear,
    Line,
    Offsets,
    Pins,
    Point,
    Primitive,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    ScenarioLayout,
    build_layout,
)
from route_analysis.turn_radius import CornerRadiusKind

FINE_ARC_STEP = 0.015
FINE_LINE_STEP = 0.06
COARSE_ARC_STEP = 0.09
COARSE_LINE_STEP = 0.18
"""Search coarse, verify fine: bisection only needs feasibility, the reading needs precision."""

FEASIBLE_SLACK = 4e-4
SHRINK_START_FRACTION = 0.25
"""First shrink step, as a fraction of the widest solved dimension's search span.

ARBITRARY, tuned across the eight scenarios: wide enough that the early cycles cost only
a handful of evaluations, narrow enough that the first wall is met before any dimension
has given up much ground.
"""
SHRINK_LEVELS = 7
"""Halvings of the shrink step; the last resolves to about 20 mm, which ``_tighten`` refines."""
TIGHTEN_STEPS = 12
GUARD_ROUNDS = 6
GUARD_GAIN = 2.0
"""The guard widens every solved dimension by the shortfall times this, plus a hair.

The coarse search over-reads feasibility by a few centimetres and one unit of shortfall
per round closed the gap too slowly: three rounds left the centreline road for a two-way
U-turn with a pinned aisle still short, and the Pareto solve then fell back onto it as
"feasible by construction".
"""
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
            # A body that crosses a wall while its centre is still inside reads a flat 0.0
            # from the ring distance. Flat is fatal to the offset descent: every probe reads
            # the same, so it never moves off a start that sits in the breach. Without the
            # centreline cap -- which a pinned dimension or offset removes -- the search does
            # start there, so give it a slope: how far the deepest corner sits outside.
            crossing = inside & (values <= 0.0)
            if crossing.any():
                corner_points = shapely.points(samples.corners[crossing].reshape(-1, 2))
                depth = shapely.distance(corner_points, region).reshape(-1, 4).max(axis=1)
                for slot, pose in enumerate(np.flatnonzero(crossing)):
                    if depth[slot] <= 0.0:
                        # Every corner inside yet the ring is crossed: a concave vertex of
                        # the region pokes into the body. Rare, and only these few poses
                        # pay for the exact measurement.
                        depth[slot] = _vertex_penetration(samples.corners[pose], region_coords)
                values[crossing] = -np.maximum(depth, MIN_PENETRATION)
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


def offset_specs(
    inputs: ScenarioInputs, dims: RoadDimensions, pins: Pins = NO_PINS
) -> tuple[OffsetSpec, ...]:
    """Which offsets are free to move under these inputs, and how far each may travel.

    A pinned offset is not free: it keeps the operator's value and drops out of the list.
    """

    half = inputs.dimensions.width / 2

    def room(width: float) -> float:
        return max(0.0, width / 2 - half - inputs.threshold - 0.004)

    specs: list[OffsetSpec] = []
    if inputs.scenario is Scenario.UTURN:
        specs.append(OffsetSpec("yc", -1.4, max(0.4, dims.d - 0.15)))
    if not inputs.pareto:
        return tuple(spec for spec in specs if spec.key not in pins.offsets)
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
    return tuple(spec for spec in specs if spec.key not in pins.offsets)


def relevant_pins(inputs: ScenarioInputs, pins: Pins) -> Pins:
    """Drop pins that name nothing this layout solves or frees.

    A pinned shared-leg offset, or a dimension another scenario owns, must not change the
    answer: geometry already ignores the value, so the pin has to be ignored too --
    otherwise it would still count as "a lateral offset is pinned" and lift the centreline
    cap for no reason. The sidebar never offers those pins; the API is kept honest here.
    """

    free = {spec.key for spec in offset_specs(inputs, RoadDimensions())}
    return Pins(
        dims=pins.dims & set(SOLVED_KEYS[inputs.scenario]),
        offsets=pins.offsets & free,
    )


def initial_offsets(
    inputs: ScenarioInputs, given: Offsets = CENTRED, pins: Pins = NO_PINS
) -> Offsets:
    """Where the offset search starts: centred, except that pinned keys keep their value."""

    start = Offsets(yc=0.25 if inputs.scenario is Scenario.UTURN else 0.0)
    for key in pins.offsets:
        start = start.with_value(key, getattr(given, key))
    return start


def optimise_offsets(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    steps: Steps,
    *,
    cheap: bool,
    given: Offsets = CENTRED,
    pins: Pins = NO_PINS,
) -> tuple[Offsets, float]:
    """Coordinate descent: scan one variable at a time, shrink its bracket, repeat.

    The cheap pass is what the search calls hundreds of times: one descent at the given
    resolution. The reported pass finds the basin at coarse resolution -- descending from
    the centred start and, when a coarse joint grid shows a clearly different corner,
    from there as well -- and only then polishes the winner at ``steps``: one narrowed
    axis round and a scan along the diagonals of every pair of offsets, which is where a
    ridge between two coupled legs leaves the axis scans a few centimetres short.
    """

    specs = offset_specs(inputs, dims, pins)
    start = initial_offsets(inputs, given, pins)
    if not specs:
        return start, evaluate(inputs, dims, start, steps).clearance
    if cheap:
        return _descend_offsets(inputs, dims, steps, specs, start, rounds=2, samples=5)
    centred = _descend_offsets(inputs, dims, COARSE, specs, start, rounds=3, samples=9)[0]
    candidates = [start, centred]
    if len(specs) >= 2:
        seed = _seed_offsets(inputs, dims, start, specs)
        if seed is not None:
            candidates.append(
                _descend_offsets(inputs, dims, COARSE, specs, seed, rounds=2, samples=9)[0]
            )
    # The coarse winner is only a guess at the fine landscape: the centred start stays a
    # candidate, or a coarse peak a few centimetres off the true one loses the threshold
    # the centreline road already met. For a U-turn that road met it at *its* start of
    # arc, not at the default one, so the centreline optimum is a candidate as well --
    # the feasible pocket of the arc start can be narrower than the coarse grid step.
    if inputs.pareto and inputs.scenario is Scenario.UTURN:
        centreline = replace(inputs, condition=Condition.CENTRELINE)
        kept = Pins(offsets=pins.offsets & {"yc"})
        candidates.append(_report_clearance(centreline, dims, given, kept)[0])
    offsets = max(candidates, key=lambda item: evaluate(inputs, dims, item, steps).clearance)
    offsets, best = _descend_offsets(
        inputs, dims, steps, specs, offsets, rounds=2, samples=9, bracket=DIAGONAL_FRACTION
    )
    return _diagonal_pass(inputs, dims, steps, specs, offsets, best, DIAGONAL_SAMPLES)


def _descend_offsets(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    steps: Steps,
    specs: tuple[OffsetSpec, ...],
    offsets: Offsets,
    *,
    rounds: int,
    samples: int,
    bracket: float = 1.0,
) -> tuple[Offsets, float]:
    """``bracket`` narrows the first scan to that fraction of each range around the start."""

    brackets = []
    for spec in specs:
        half = (spec.high - spec.low) * bracket / 2
        centre = getattr(offsets, spec.key)
        brackets.append(
            (spec.low, spec.high)
            if bracket >= 1.0
            else (max(spec.low, centre - half), min(spec.high, centre + half))
        )
    best = evaluate(inputs, dims, offsets, steps).clearance
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


def _diagonal_pass(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    steps: Steps,
    specs: tuple[OffsetSpec, ...],
    offsets: Offsets,
    best: float,
    samples: int,
) -> tuple[Offsets, float]:
    """Scan along the diagonals of every pair of offsets from where the axes stopped.

    Where two legs trade against each other the optimum lies on a ridge that runs
    diagonally through the offset plane, and a scan along one axis at a time zig-zags up
    it in ever smaller steps until the bracket closes -- stopping a few centimetres short.
    That short-fall is enough for the same road to read safe from the solve and warning
    from the full-pinned check of the very numbers the solve reported. One pass along
    each pair's two diagonals reaches the ridge the axes cannot.
    """

    for first in range(len(specs)):
        for second in range(first + 1, len(specs)):
            a, b = specs[first], specs[second]
            span_a = (a.high - a.low) * DIAGONAL_FRACTION
            span_b = (b.high - b.low) * DIAGONAL_FRACTION
            for sign in (1.0, -1.0):
                base_a = getattr(offsets, a.key)
                base_b = getattr(offsets, b.key)
                chosen = offsets
                for step in range(-samples, samples + 1):
                    if step == 0:
                        continue
                    t = step / samples
                    candidate = offsets.with_value(
                        a.key, min(max(base_a + span_a * t, a.low), a.high)
                    ).with_value(
                        b.key, min(max(base_b + sign * span_b * t, b.low), b.high)
                    )
                    probe = evaluate(inputs, dims, candidate, steps)
                    if probe.clearance > best + 1e-6:
                        best = probe.clearance
                        chosen = candidate
                offsets = chosen
    return offsets, best


SEED_POINTS = 4
"""Joint grid points per free offset the reported pass starts from, bounds included."""
SEED_MARGIN = 0.01
"""How much better (m) a grid corner must read before it earns a descent of its own."""
DIAGONAL_FRACTION = 0.28
"""Half-span of the closing diagonal scan, as a fraction of each offset's range."""
DIAGONAL_SAMPLES = 6
"""Probes per side along each diagonal of the closing scan."""


def _seed_offsets(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    start: Offsets,
    specs: tuple[OffsetSpec, ...],
) -> Offsets | None:
    """The best corner of a coarse joint grid, or ``None`` if the start already wins.

    Coordinate descent walks one axis at a time and stops on the first ridge it meets.
    With the legs coupled -- a T junction's trunk offset and branch offset trade against
    each other -- the ridge it stops on depends on where it started, and the cheap search
    pass and the reported pass can then land in different basins: the search accepts a
    road on the strength of one basin and the report reads the other, below threshold.
    A joint grid at coarse resolution is cheap enough here (this pass runs a handful of
    times per solve, not hundreds). The seed is a second start, not a replacement: a
    better corner does not guarantee a better basin, so both descents run -- at coarse
    resolution -- and the higher finish is the one polished.
    """

    if len(specs) > 3:
        return None
    axes = [
        [
            spec.low + (spec.high - spec.low) * step / (SEED_POINTS - 1)
            for step in range(SEED_POINTS)
        ]
        for spec in specs
    ]
    seed = None
    # A corner that only just beats the start is the same basin seen at coarse
    # resolution; the descent from the start reaches it anyway.
    seed_best = evaluate(inputs, dims, start, COARSE).clearance + SEED_MARGIN
    for values in product(*axes):
        candidate = start
        for spec, value in zip(specs, values, strict=True):
            candidate = candidate.with_value(spec.key, value)
        clearance = evaluate(inputs, dims, candidate, COARSE).clearance
        if clearance > seed_best + 1e-6:
            seed_best = clearance
            seed = candidate
    return seed


def _feasible(
    inputs: ScenarioInputs, dims: RoadDimensions, given: Offsets, pins: Pins
) -> bool:
    target = inputs.threshold - FEASIBLE_SLACK
    if inputs.optimises_offsets:
        return (
            optimise_offsets(inputs, dims, COARSE, cheap=True, given=given, pins=pins)[1]
            >= target
        )
    return (
        evaluate(inputs, dims, initial_offsets(inputs, given, pins), COARSE).clearance
        >= target
    )


def _shrink_wrap(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    keys: tuple[str, ...],
    brackets: dict[str, tuple[float, float]],
    given: Offsets,
    pins: Pins,
) -> RoadDimensions:
    """Draw every solved dimension in at the same rate until each one meets its own wall.

    Compressing them one at a time lands on an extreme vertex of the frontier instead of
    a road anyone would build. Whichever dimension goes first is minimised while the rest
    still sit at their search ceilings, where they constrain nothing, so it takes the whole
    budget and the others can no longer move: two-way crossback solved the turn-out road to
    7.02 m beside a 0.36 m dip -- past its own 6.93 m search ceiling -- when 2.31 m and
    2.72 m both fit. Reordering does not help, it just hands the blow-up to whichever
    dimension now goes first (turn-out first gives a 6.12 m reverse lane instead).

    Stepping them down together keeps the coupling in view the whole way, so each one stops
    where its own wall is rather than where another one's slack let it run. A step is taken
    only when the result is feasible, which is what keeps this honest on the non-monotone
    dimensions: widening a U-turn aisle also widens the turning circle, so the swept apex
    reaches further into the end head and "wider" can stop fitting. Feasibility is therefore
    not a half-line, and a step refused at one size may still be taken at half of it.
    """

    step = SHRINK_START_FRACTION * max(
        getattr(dims, key) - brackets[key][0] for key in keys
    )
    for _ in range(SHRINK_LEVELS):
        if step <= 0.0:
            break
        moved = True
        while moved:
            moved = False
            for key in keys:
                current = getattr(dims, key)
                candidate = max(brackets[key][0], current - step)
                if candidate >= current - 1e-9:
                    continue
                if _feasible(inputs, dims.with_value(key, candidate), given, pins):
                    dims = dims.with_value(key, candidate)
                    moved = True
        step /= 2.0
    return dims


@dataclass(frozen=True, slots=True)
class ForwardSolution:
    dims: RoadDimensions | None
    ceiling_clearance: float
    """Best clearance reachable with every solved dimension at its search ceiling.

    When nothing solves this is the threshold's upper bound, which lets the view say how
    far the threshold has to come down before there is an answer instead of just
    reporting failure.
    """


def _centreline_ceiling(
    inputs: ScenarioInputs, given: RoadDimensions, given_offsets: Offsets, pins: Pins
) -> RoadDimensions | None:
    """The centreline answer, which caps the extreme one dimension by dimension.

    An extreme condition only ever adds freedom: every lateral offset is free to stay at
    zero, so the centreline road is always feasible here too. Letting the extreme solve
    run past it means telling the operator that permission to leave the centreline made a
    road *wider*, which is not something anyone can act on. It happened in ten of the
    twelve forward variants, and not always marginally -- two-way crossback asked for a
    4.46 m turn-out road against the centreline's 2.51 m. The offset optimiser is the
    greedy one here: early in the descent the road is still wide, so a large offset costs
    nothing and buys enough clearance for the other dimensions to shrink hard, and the
    dimension that has to contain that offset can then never come back down.

    The cap does take real answers off the table. With the turn-out road and the dip held
    at their centreline values, the one-way reverse lane genuinely cannot go below the
    centreline figure either, where an uncapped solve would have offered a narrower lane
    in exchange for a 10 cm deeper dip. So the extreme condition now reports what offsets
    buy *on top of* the centreline road, not a different road arrived at by trading one
    dimension away for another.

    Pinned dimensions stay pinned in the centreline solve too, so the cap is the centreline
    road built around the same givens. A pinned lateral offset breaks the argument -- the
    truck is no longer free to stay on the centreline -- so no cap applies then. A pinned
    start-of-arc is carried across: the U-turn arc start is free under both conditions.
    """

    if not inputs.pareto or pins.lateral_offsets:
        return None
    return solve_forward(
        replace(inputs, condition=Condition.CENTRELINE),
        given,
        given_offsets,
        Pins(dims=pins.dims, offsets=pins.offsets & {"yc"}),
    ).dims


def solve_forward(
    inputs: ScenarioInputs,
    given: RoadDimensions,
    given_offsets: Offsets = CENTRED,
    pins: Pins = NO_PINS,
) -> ForwardSolution:
    """A set of road dimensions none of which can shrink alone. The answer sits on the

    Pareto frontier and is not unique. Pinned dimensions keep their given value and the
    rest are driven to their limit around them; pinned offsets are held where the operator
    put them. With everything pinned there is nothing to solve and the givens are reported
    as they are.
    """

    pins = relevant_pins(inputs, pins)
    keys = tuple(key for key in SOLVED_KEYS[inputs.scenario] if key not in pins.dims)
    if not keys:
        return ForwardSolution(given, _report_clearance(inputs, given, given_offsets, pins)[1])
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
    cap = _centreline_ceiling(inputs, given, given_offsets, pins)
    if cap is not None:
        brackets = {
            key: (low, max(low, min(high, getattr(cap, key))) if key in keys else high)
            for key, (low, high) in brackets.items()
        }
    dims = replace(
        given,
        **{key: brackets[key][1] for key in keys},
    )
    ceiling = _report_clearance(inputs, dims, given_offsets, pins)[1]
    if not _feasible(inputs, dims, given_offsets, pins):
        if cap is not None:
            # The centreline road is feasible under an extreme condition by construction,
            # every offset being free to stay at zero, so report it rather than claim there
            # is no answer. The cheap feasibility heuristic can still read it as short: the
            # centreline solve leaves its answer sitting exactly on the threshold, and a
            # coarse offset search under-reads what the offsets can actually reach.
            return ForwardSolution(dims, ceiling)
        return ForwardSolution(None, ceiling)
    dims = _shrink_wrap(inputs, dims, keys, brackets, given_offsets, pins)
    check = _report_clearance(inputs, dims, given_offsets, pins)[1]
    for _ in range(GUARD_ROUNDS):
        if check >= inputs.threshold:
            break
        bump = GUARD_GAIN * (inputs.threshold - check) + 0.006
        dims = replace(
            dims,
            **{
                key: min(getattr(dims, key) + bump, brackets[key][1])
                for key in keys
            },
        )
        check = _report_clearance(inputs, dims, given_offsets, pins)[1]
    if check < inputs.threshold and cap is not None:
        # Capped at the centreline road, the bump has nowhere left to climb, so it can run
        # out of rounds still short. That ceiling is feasible by construction, so fall back
        # onto it rather than report a road that does not make the threshold.
        dims = replace(dims, **{key: brackets[key][1] for key in keys})
    return ForwardSolution(
        _tighten(inputs, dims, keys, brackets, given_offsets, pins), ceiling
    )


_REPORT_CACHE: dict[
    tuple[ScenarioInputs, RoadDimensions, Offsets, Pins, float], tuple[Offsets, float]
] = {}
REPORT_CACHE_SIZE = 256


def _report_clearance(
    inputs: ScenarioInputs, dims: RoadDimensions, given: Offsets, pins: Pins
) -> tuple[Offsets, float]:
    """The evaluation that gets reported; the Pareto condition and U-turns optimise first.

    One solve asks for the same road several times over -- the guard's last check and
    the tightening's first read, the tightening's last read and the final report -- and
    each reported pass is the expensive one, so the answer is kept for the exact inputs.
    """

    # The end-wall padding is module state the fixture cross-check patches; it shapes the
    # region, so it belongs in the key.
    key = (inputs, dims, given, pins, scenario_geometry.CAP_PAD)
    cached = _REPORT_CACHE.get(key)
    if cached is not None:
        return cached
    if inputs.optimises_offsets:
        result = optimise_offsets(inputs, dims, FINE, cheap=False, given=given, pins=pins)
    else:
        offsets = initial_offsets(inputs, given, pins)
        result = offsets, evaluate(inputs, dims, offsets, FINE).clearance
    if len(_REPORT_CACHE) >= REPORT_CACHE_SIZE:
        _REPORT_CACHE.clear()
    _REPORT_CACHE[key] = result
    return result


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
    given: Offsets,
    pins: Pins,
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

    offsets, before = _report_clearance(inputs, dims, given, pins)
    if before < inputs.threshold:
        return dims
    original = dims
    for key in keys:
        low = brackets[key][0]
        current = getattr(dims, key)
        if current - low < 1e-3:
            continue
        frozen = _clamp(offsets, offset_specs(inputs, dims, pins))
        floor = evaluate(inputs, dims.with_value(key, low), frozen, FINE)
        if floor.clearance >= inputs.threshold:
            dims = dims.with_value(key, low)
            continue
        lower, upper = low, current
        for _ in range(TIGHTEN_STEPS):
            middle = (lower + upper) / 2
            candidate = dims.with_value(key, middle)
            frozen = _clamp(offsets, offset_specs(inputs, candidate, pins))
            if evaluate(inputs, candidate, frozen, FINE).clearance >= inputs.threshold:
                upper = middle
            else:
                lower = middle
        dims = dims.with_value(key, upper)
    if _report_clearance(inputs, dims, given, pins)[1] < inputs.threshold:
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

    Every lateral offset the layout has is scanned, pinned ones included: the band is what
    tells the operator how far a pinned leg could still be moved.

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
    trunk_reach: float | None = None
    """出弯主路深度 for the stub scenario: measured off the sweep, not solved for."""
    pins: Pins = NO_PINS
    solved_keys: tuple[str, ...] = ()
    """The dimensions this result actually solved; empty when everything was pinned or

    when no answer was found.
    """

    @property
    def solved(self) -> bool:
        return bool(self.solved_keys) and not self.infeasible

    @property
    def margin(self) -> float:
        """Headroom against the threshold: positive to spare, negative short."""

        return self.min_clearance - self.inputs.threshold


def _centred_clearance(
    inputs: ScenarioInputs, dims: RoadDimensions, given: Offsets, pins: Pins
) -> float:
    """The comparison row: what is left if the truck stays on the centreline after all.

    It sits beside the optimised reading, so a breach has to report its real negative
    depth. That means the detail path, not the search path, which reads any crossing as
    a flat zero. The start of the U-turn arc is chosen by the same optimiser the
    centreline condition uses, so this row and that variant agree by construction; a
    pinned arc start stays pinned, since it is not a lateral offset.
    """

    centre = replace(inputs, condition=Condition.CENTRELINE)
    kept = Pins(offsets=pins.offsets & {"yc"})
    offsets, _ = _report_clearance(centre, dims, given, kept)
    return evaluate(inputs, dims, offsets, FINE, detail=True).clearance


def trunk_reach(
    layout: ScenarioLayout, vehicle: VehicleDimensions, branch_width: float, threshold: float
) -> float:
    """How far along the trunk the reverse swing runs, measured from the branch mouth.

    Reported rather than searched for. It cannot be a wall the solver squeezes: the trunk
    carries on in both directions, and in a two-way layout the mirrored maneuver drives away
    through the very side the other one swings into, so a wall placed there would block the
    departure it is supposed to size. Measured over the reverse poses only -- the forward leg
    is the truck leaving, not the swing -- and the widest of those is the moment the wheels
    come straight, which is exactly the reading being asked for.
    """

    widest = 0.0
    for maneuver in layout.maneuvers:
        samples = sample_poses(maneuver.primitives, vehicle, FINE)
        if not len(samples):
            continue
        reversing = ~samples.gear_is_drive
        if not reversing.any():
            continue
        along = samples.corners[reversing][:, :, 0]
        widest = max(widest, float(np.abs(along).max()))
    return max(0.0, widest - branch_width / 2 + threshold)


UTURN_AISLE_OFFSETS = ("e1", "e2", "eo")
"""The offsets that move a U-turn aisle path; negative is outwards for all three."""


def _leaning_outwards(
    inputs: ScenarioInputs, dims: RoadDimensions, given: Offsets, pins: Pins
) -> Offsets:
    """The start offsets with every free aisle offset at its outer limit.

    That is where the U-turn circle is widest, so it is the honest place to ask whether
    the radius fits at all. Pinned offsets stay where the operator put them.
    """

    offsets = initial_offsets(inputs, given, pins)
    for spec in offset_specs(inputs, dims, pins):
        if spec.key in UTURN_AISLE_OFFSETS:
            offsets = offsets.with_value(spec.key, spec.low)
    return offsets


def _required_lane_width(
    inputs: ScenarioInputs, dims: RoadDimensions, given: Offsets, pins: Pins
) -> float:
    """The narrowest aisle that still holds the half circle, offsets leaning outwards.

    ``2R - b`` is the answer only with the paths on their centrelines; with the aisle
    offsets free the circle can grow into the outer walls and the aisle can be narrower.
    Buildability is monotone in the aisle width, so bisect it rather than write a second
    formula that has to track how much room the offsets get.
    """

    def fits(width: float) -> bool:
        trial = dims.with_value("w", width)
        return build_layout(inputs, trial, _leaning_outwards(inputs, trial, given, pins)).buildable

    if fits(dims.w):
        return dims.w
    high = 2 * inputs.radius - dims.b + 1e-6
    # A pinned offset leaning inwards shrinks the circle, so even the centreline width
    # can fall short; widen the bracket until it holds before bisecting.
    for _ in range(8):
        if fits(high):
            break
        high += inputs.radius
    else:
        return high
    low = dims.w
    for _ in range(40):
        middle = (low + high) / 2
        if fits(middle):
            high = middle
        else:
            low = middle
    return high


def _unbuildable(
    inputs: ScenarioInputs,
    dims: RoadDimensions,
    offsets: Offsets,
    layout: ScenarioLayout,
    radii: dict[CornerRadiusKind, float],
    pins: Pins,
) -> ScenarioResult:
    """The U-turn half circle does not fit between the aisles: report the width it needs."""

    clearance = -0.5 - layout.radius_shortfall
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
        required_lane_width=_required_lane_width(inputs, dims, offsets, pins),
        infeasible=True,
        pins=pins,
    )


def solve_scenario(
    inputs: ScenarioInputs,
    given: RoadDimensions,
    offsets: Offsets = CENTRED,
    pins: Pins = NO_PINS,
) -> ScenarioResult:
    """One full estimate: the limiting dimensions around whatever the operator pinned.

    Under the centreline condition nothing can be pinned and every lateral offset is zero,
    so ``offsets`` and ``pins`` are ignored there. Under the Pareto condition the pinned
    keys are taken as given; with every solved dimension pinned the road is simply checked
    and the status is the verdict.
    """

    pins = relevant_pins(inputs, pins) if inputs.pareto else Pins()
    if not inputs.pareto:
        offsets = Offsets()
    radii = corner_radii(inputs.dimensions, inputs.radius)
    ceiling: float | None = None
    keys = tuple(key for key in SOLVED_KEYS[inputs.scenario] if key not in pins.dims)
    if inputs.scenario is Scenario.UTURN and "w" not in keys:
        # The aisle pitch is fixed by what the operator typed, so whether the half circle
        # fits is settled before any search: say so with the width it would need, rather
        # than letting the search for the head depth fail and report a generic no-answer.
        # Free aisle offsets can lean both legs outwards and widen the circle, so the
        # question is asked with them at their outer limit.
        widest = _leaning_outwards(inputs, given, offsets, pins)
        fixed = build_layout(inputs, given, widest)
        if not fixed.buildable:
            return _unbuildable(inputs, given, widest, fixed, radii, pins)
    if keys:
        solution = solve_forward(inputs, given, offsets, pins)
        if solution.dims is None:
            start = (
                _leaning_outwards(inputs, given, offsets, pins)
                if inputs.scenario is Scenario.UTURN
                else initial_offsets(inputs, offsets, pins)
            )
            layout = build_layout(inputs, given, start)
            return ScenarioResult(
                inputs=inputs,
                dims=given,
                offsets=start,
                layout=layout,
                probe=Probe(clearance=solution.ceiling_clearance),
                status=ClearanceStatus.UNAVAILABLE,
                min_clearance=solution.ceiling_clearance,
                corner_radii=radii,
                bottleneck=(
                    "在当前固定的尺寸与偏移下找不到可行的道路极限尺寸。"
                    if pins.dims or pins.offsets
                    else "在给定车辆参数下找不到可行的道路极限尺寸。"
                ),
                infeasible=True,
                threshold_ceiling=max(0.0, solution.ceiling_clearance),
                pins=pins,
            )
        dims = solution.dims
        ceiling = solution.ceiling_clearance
    else:
        dims = given

    offsets, _clearance = _report_clearance(inputs, dims, offsets, pins)
    layout = build_layout(inputs, dims, offsets)
    if not layout.buildable:
        return _unbuildable(inputs, dims, offsets, layout, radii, pins)

    probe = evaluate_layout(layout, inputs.dimensions, FINE, detail=True)
    bands: tuple[OffsetBand, ...] = ()
    centred: float | None = None
    reach = (
        trunk_reach(layout, inputs.dimensions, dims.wv, inputs.threshold)
        if inputs.scenario is Scenario.STUBBACK
        else None
    )
    if inputs.pareto:
        centred = _centred_clearance(inputs, dims, offsets, pins)
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
        trunk_reach=reach,
        pins=pins,
        solved_keys=keys,
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
    ux: np.ndarray
    uy: np.ndarray
    """Travel direction per pose. The heading follows it in D and opposes it in R, which is

    what lets a drawing tell the nose end from the tail end rather than the leading end.
    """


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
                ux=samples.ux,
                uy=samples.uy,
            )
        )
    return tuple(traces)
