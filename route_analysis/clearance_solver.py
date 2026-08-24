"""Segment, bottleneck, offset-band and corner solvers behind the clearance headroom view."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from route_analysis.analysis import analyze_path
from route_analysis.clearance_geometry import FittedCorner, build_corner_poses, fit_corner
from route_analysis.geometry import build_lane_area, vehicle_polygon
from route_analysis.models import (
    AnalysisSettings,
    ClearanceStatus,
    Lane,
    PosePoint,
    VehicleDimensions,
)
from route_analysis.turn_radius import TurnSide, detect_turns

SEARCH_RADIUS = 1.5
"""Widest lateral offset, in metres, the band search will consider either way."""

COARSE_STEPS = 16
BISECTION_STEPS = 10
UNBOUNDED = float("inf")
CONFLICT_TOLERANCE = 0.005
"""Bands closer than this are treated as agreeing; a hair of float noise is not a conflict."""


class SegmentRole(StrEnum):
    STRAIGHT = "straight"
    TURN = "turn"


def _left_normal(heading: float) -> tuple[float, float]:
    return -math.sin(heading), math.cos(heading)


def _shift(pose: PosePoint, offset: float) -> PosePoint:
    if pose.yaw is None or offset == 0.0:
        return pose
    normal_x, normal_y = _left_normal(pose.yaw)
    return PosePoint(pose.x + offset * normal_x, pose.y + offset * normal_y, pose.yaw)


@dataclass(frozen=True, slots=True)
class PathSegment:
    """One straight run or one turn along the analysed path."""

    index: int
    role: SegmentRole
    start_pose: int
    end_pose: int
    start_label: int
    end_label: int
    start_progress: float
    end_progress: float
    side: TurnSide | None = None
    lane_id: str | None = None
    lane_name: str | None = None
    lane_width: float | None = None

    @property
    def inward_sign(self) -> float | None:
        """Which lateral direction points into the turn, or None on a straight run."""

        if self.side is None:
            return None
        return 1.0 if self.side is TurnSide.LEFT else -1.0

    def describe_offset(self, value: float) -> str:
        """Word an offset the way the turn reads it, avoiding a sign that flips meaning."""

        if abs(value) < 5e-3:
            return "居中"
        sign = self.inward_sign
        if sign is None:
            direction = "左" if value > 0 else "右"
        else:
            direction = "向内" if value * sign > 0 else "向外"
        return f"{direction} {abs(value):.2f}"

    @property
    def turn_text(self) -> str:
        if self.side is None:
            return ""
        return " 左转" if self.side is TurnSide.LEFT else " 右转"

    @property
    def short_label(self) -> str:
        return f"{self.start_label}–{self.end_label}{self.turn_text}"

    @property
    def label(self) -> str:
        head = f"点位 {self.short_label}"
        return f"{head} · {self.lane_name}" if self.lane_name else head


@dataclass(frozen=True, slots=True)
class ProfileSample:
    progress: float
    clearance: float
    segment_index: int | None


@dataclass(frozen=True, slots=True)
class OffsetBand:
    """Lateral offsets, in metres left of travel, that keep one segment clear."""

    segment_index: int
    low: float
    high: float
    best: float
    best_clearance: float
    feasible: bool = True

    @property
    def width(self) -> float:
        return max(0.0, self.high - self.low) if self.feasible else 0.0

    def contains(self, offset: float) -> bool:
        return self.feasible and self.low <= offset <= self.high

    def nearest_edge(self, offset: float) -> float | None:
        """Smallest move from ``offset`` that lands inside this band."""

        if not self.feasible or self.contains(offset):
            return None
        return self.low if offset < self.low else self.high


@dataclass(frozen=True, slots=True)
class CoupledBand:
    """One segment's band after intersecting the demands of both adjoining turns."""

    segment_index: int
    low: float
    high: float
    sources: tuple[int, ...]
    feasible: bool = True
    conflicting: bool = False

    @property
    def shortfall(self) -> float:
        """How far apart the adjoining turns' demands are, when they cannot both be met."""

        return max(0.0, self.low - self.high) if self.conflicting else 0.0


@dataclass(frozen=True, slots=True)
class WidthZones:
    """The three-zone ruler for one bottleneck: infeasible, needs offset, clears centred."""

    lane_id: str
    lane_name: str
    measured: float
    offset_limit: float | None
    centred_limit: float | None
    scale_low: float
    scale_high: float


CORNER_NAMES = {
    (True, True): "前内角",
    (True, False): "前外角",
    (False, True): "后内角",
    (False, False): "后外角",
}


def constraining_feature(
    pose: PosePoint,
    dimensions: VehicleDimensions,
    area: BaseGeometry,
    side: TurnSide | None,
) -> str:
    """Name whichever part of the footprint sits closest to the traversable boundary.

    The design's whole point is that the constraint is usually the reversing side's rear
    outer corner rather than the vehicle's width, so the ranking has to say which it is.
    """

    if pose.yaw is None:
        return "—"
    polygon = vehicle_polygon(pose, dimensions)
    corners = list(polygon.exterior.coords)[:4]
    if area.covers(polygon):
        distances = [Point(x, y).distance(area.boundary) for x, y in corners]
        ordered = sorted(range(4), key=lambda position: distances[position])
        if distances[ordered[0]] - polygon.boundary.distance(area.boundary) > 0.02:
            return "车身侧边"
        if distances[ordered[1]] - distances[ordered[0]] < 0.01:
            return "车身侧边"
    else:
        penetration = [Point(x, y).distance(area) for x, y in corners]
        ordered = sorted(range(4), key=lambda position: -penetration[position])
        if penetration[ordered[0]] <= 1e-9:
            return "车身侧边"
        if penetration[ordered[0]] - penetration[ordered[1]] < 0.005:
            return "车身侧边"
    index = ordered[0]
    front = index in (0, 1)
    left = index in (0, 3)
    if side is None:
        return f"{'前' if front else '后'}{'左' if left else '右'}角"
    inner = left if side is TurnSide.LEFT else not left
    return CORNER_NAMES[(front, inner)]


@dataclass(frozen=True, slots=True)
class Bottleneck:
    """One segment ranked by how little room it leaves."""

    rank: int
    segment: PathSegment
    clearance: float
    pose_index: int
    pose: PosePoint
    progress: float
    required_offset: float | None
    inside_band: bool
    band_feasible: bool
    feature: str

    @property
    def offset_text(self) -> str:
        if not self.band_feasible:
            return "无可行偏置"
        if self.inside_band or self.required_offset is None:
            return "带内"
        return self.segment.describe_offset(self.required_offset)


@dataclass(frozen=True, slots=True)
class ClearanceAnalysis:
    """Everything the clearance headroom view reads for one dispatched path."""

    status: ClearanceStatus
    segments: tuple[PathSegment, ...]
    profile: tuple[ProfileSample, ...]
    bottlenecks: tuple[Bottleneck, ...]
    bands: tuple[OffsetBand, ...]
    coupled: tuple[CoupledBand, ...]
    threshold: float
    minimum_clearance: float | None
    deepest_breach: float | None
    outside_band_segments: int
    narrowest_band: OffsetBand | None
    suggested_clearance: float | None
    analyzed_samples: int
    pose_count: int
    recommended_offsets: dict[int, float]

    def band_for(self, segment_index: int) -> OffsetBand | None:
        return next((band for band in self.bands if band.segment_index == segment_index), None)

    def coupled_for(self, segment_index: int) -> CoupledBand | None:
        return next((item for item in self.coupled if item.segment_index == segment_index), None)


class LaneContext:
    """Enabled lane areas, cached so width searches only rebuild the lane under test."""

    def __init__(self, lanes: Sequence[Lane], settings: AnalysisSettings) -> None:
        self._settings = settings
        self._lanes = tuple(lane for lane in lanes if lane.enabled)
        self._areas = {
            lane.id: build_lane_area(
                lane, tolerance=settings.bezier_tolerance, miter_limit=settings.miter_limit
            )
            for lane in self._lanes
        }

    @property
    def lanes(self) -> tuple[Lane, ...]:
        return self._lanes

    def area(self) -> BaseGeometry:
        return self._union(self._areas)

    @staticmethod
    def _union(areas: dict[str, BaseGeometry]) -> BaseGeometry:
        return unary_union(list(areas.values())) if areas else Point().buffer(0)

    def covering(self, pose: PosePoint) -> Lane | None:
        """Enabled lane whose own area contains this pose's reference point."""

        location = Point(pose.x, pose.y)
        for lane in self._lanes:
            if self._areas[lane.id].contains(location):
                return lane
        return None

    def area_with_width(self, lane_id: str, width: float) -> BaseGeometry:
        """Traversable area with one lane rebuilt at a different total width."""

        replacement = dict(self._areas)
        for lane in self._lanes:
            if lane.id == lane_id:
                replacement[lane_id] = build_lane_area(
                    replace(lane, width=width),
                    tolerance=self._settings.bezier_tolerance,
                    miter_limit=self._settings.miter_limit,
                )
                break
        return self._union(replacement)


def _segment_lengths(poses: Sequence[PosePoint]) -> tuple[list[float], float]:
    lengths = [
        math.dist((first.x, first.y), (second.x, second.y))
        for first, second in pairwise(poses)
    ]
    return lengths, sum(lengths)


def _progress_table(poses: Sequence[PosePoint]) -> list[float]:
    lengths, total = _segment_lengths(poses)
    table = [0.0]
    travelled = 0.0
    for length in lengths:
        travelled += length
        table.append(travelled / total if total > 0 else 0.0)
    return table


def _tighten_turn(
    poses: Sequence[PosePoint], start: int, end: int
) -> tuple[int, int]:
    """Trim a detected turn back to the samples that actually change heading.

    ``detect_turns`` bounds a section by direction reversals, so a straight run between
    two turns of opposite sense lands inside the first one. Trimming keeps a straight
    run a straight run.
    """

    deltas = []
    for index in range(start, end):
        first, second = poses[index].yaw, poses[index + 1].yaw
        deltas.append(0.0 if first is None or second is None else abs(second - first))
    if not deltas:
        return start, end
    peak = max(deltas)
    if peak <= 0:
        return start, end
    epsilon = max(1e-6, peak * 0.02)
    head = 0
    while head < len(deltas) and deltas[head] <= epsilon:
        head += 1
    tail = len(deltas)
    while tail > head and deltas[tail - 1] <= epsilon:
        tail -= 1
    if head >= tail:
        return start, end
    return start + head, start + tail


def split_segments(
    poses: Sequence[PosePoint],
    *,
    turn_threshold: float,
    sample_labels: Sequence[int] | None = None,
) -> tuple[PathSegment, ...]:
    """Alternate straight runs and detected turns across the whole path."""

    if len(poses) < 2:
        return ()
    progress = _progress_table(poses)
    labels = list(sample_labels) if sample_labels is not None else list(range(1, len(poses) + 1))
    turns = sorted(detect_turns(poses, threshold=turn_threshold), key=lambda turn: turn.start_index)

    boundaries: list[tuple[int, int, SegmentRole, TurnSide | None]] = []
    cursor = 0
    for turn in turns:
        start, end = _tighten_turn(poses, turn.start_index, turn.end_index)
        if start <= cursor:
            start = cursor
        if end <= start:
            continue
        if start > cursor:
            boundaries.append((cursor, start, SegmentRole.STRAIGHT, None))
        side = TurnSide.LEFT if turn.cumulative_yaw > 0 else TurnSide.RIGHT
        boundaries.append((start, end, SegmentRole.TURN, side))
        cursor = end
    if cursor < len(poses) - 1:
        boundaries.append((cursor, len(poses) - 1, SegmentRole.STRAIGHT, None))

    return tuple(
        PathSegment(
            index=index,
            role=role,
            start_pose=start,
            end_pose=end,
            start_label=labels[start] if start < len(labels) else start + 1,
            end_label=labels[end] if end < len(labels) else end + 1,
            start_progress=progress[start],
            end_progress=progress[end],
            side=side,
        )
        for index, (start, end, role, side) in enumerate(boundaries)
    )


def _attach_lanes(
    segments: Sequence[PathSegment], poses: Sequence[PosePoint], context: LaneContext
) -> tuple[PathSegment, ...]:
    """Name each segment after the enabled lane covering its midpoint, when there is one."""

    attached: list[PathSegment] = []
    for segment in segments:
        midpoint = poses[(segment.start_pose + segment.end_pose) // 2]
        lane = context.covering(midpoint)
        attached.append(
            replace(
                segment,
                lane_id=lane.id if lane else None,
                lane_name=lane.name if lane else None,
                lane_width=lane.width if lane else None,
            )
        )
    return tuple(attached)


def _segment_clearance(
    poses: Sequence[PosePoint],
    segment: PathSegment,
    dimensions: VehicleDimensions,
    area: BaseGeometry,
    settings: AnalysisSettings,
    *,
    offset: float = 0.0,
) -> float:
    """Worst clearance inside one segment, optionally shifted sideways."""

    window = [_shift(pose, offset) for pose in poses[segment.start_pose : segment.end_pose + 1]]
    result = analyze_path(window, dimensions, area, settings)
    if result.minimum_clearance is None:
        return -UNBOUNDED
    return result.minimum_clearance


def _band_edge(
    evaluate: Callable[[float], float],
    inside: float,
    outside: float,
    threshold: float,
) -> float:
    """Bisect between a passing and a failing offset for the edge of a band."""

    low, high = inside, outside
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2
        if evaluate(middle) >= threshold:
            low = middle
        else:
            high = middle
    return low


def solve_band(
    poses: Sequence[PosePoint],
    segment: PathSegment,
    dimensions: VehicleDimensions,
    area: BaseGeometry,
    settings: AnalysisSettings,
    *,
    search_radius: float = SEARCH_RADIUS,
) -> OffsetBand:
    """Lateral offsets keeping one segment at or above the clearance threshold."""

    def evaluate(offset: float) -> float:
        return _segment_clearance(poses, segment, dimensions, area, settings, offset=offset)

    candidates = [
        -search_radius + 2 * search_radius * step / COARSE_STEPS for step in range(COARSE_STEPS + 1)
    ]
    scored = [(offset, evaluate(offset)) for offset in candidates]
    best_offset, best_clearance = max(scored, key=lambda item: item[1])
    threshold = settings.clearance_threshold
    if best_clearance < threshold:
        return OffsetBand(
            segment.index, best_offset, best_offset, best_offset, best_clearance, False
        )

    lower = -search_radius
    for offset, clearance in scored:
        if offset >= best_offset:
            break
        if clearance < threshold:
            lower = offset
    upper = search_radius
    for offset, clearance in reversed(scored):
        if offset <= best_offset:
            break
        if clearance < threshold:
            upper = offset

    low = (
        -search_radius
        if lower <= -search_radius and scored[0][1] >= threshold
        else _band_edge(evaluate, best_offset, lower, threshold)
    )
    high = (
        search_radius
        if upper >= search_radius and scored[-1][1] >= threshold
        else _band_edge(evaluate, best_offset, upper, threshold)
    )
    return OffsetBand(segment.index, low, high, best_offset, best_clearance)


def best_clearance(
    poses: Sequence[PosePoint],
    segment: PathSegment,
    dimensions: VehicleDimensions,
    area: BaseGeometry,
    settings: AnalysisSettings,
    *,
    search_radius: float = SEARCH_RADIUS,
) -> float:
    """Best clearance any lateral offset can buy this segment, without locating the band."""

    return max(
        _segment_clearance(
            poses,
            segment,
            dimensions,
            area,
            settings,
            offset=-search_radius + 2 * search_radius * step / COARSE_STEPS,
        )
        for step in range(COARSE_STEPS + 1)
    )


def couple_bands(
    segments: Sequence[PathSegment], bands: Sequence[OffsetBand]
) -> tuple[CoupledBand, ...]:
    """Intersect each straight run's band with the turns on either side of it.

    A connecting run carries a single offset, so a turn before it and a turn after it
    must agree. Where they cannot, the intersection is empty and the conflict is real.
    """

    by_index = {band.segment_index: band for band in bands}
    coupled: list[CoupledBand] = []
    for segment in segments:
        band = by_index.get(segment.index)
        if band is None:
            continue
        if not band.feasible:
            coupled.append(
                CoupledBand(segment.index, band.best, band.best, (segment.index,), False)
            )
            continue
        low, high = band.low, band.high
        sources = [segment.index]
        if segment.role is SegmentRole.STRAIGHT:
            for neighbour in (segment.index - 1, segment.index + 1):
                adjoining = by_index.get(neighbour)
                if adjoining is None or not adjoining.feasible:
                    continue
                if segments[neighbour].role is not SegmentRole.TURN:
                    continue
                low = max(low, adjoining.low)
                high = min(high, adjoining.high)
                sources.append(neighbour)
        conflicting = low - high > CONFLICT_TOLERANCE
        if not conflicting and high < low:
            low = high = (low + high) / 2
        coupled.append(
            CoupledBand(segment.index, low, high, tuple(sources), not conflicting, conflicting)
        )
    return tuple(coupled)


def solve_width_zones(
    poses: Sequence[PosePoint],
    segment: PathSegment,
    dimensions: VehicleDimensions,
    context: LaneContext,
    settings: AnalysisSettings,
    *,
    search_radius: float = SEARCH_RADIUS,
) -> WidthZones | None:
    """Widths at which this segment clears centred, clears only offset, or cannot clear.

    The search varies the covering lane's own total width and rebuilds the traversable
    area, so every reading comes from the same swept-footprint chain the view already uses.
    """

    if segment.lane_id is None or segment.lane_width is None:
        return None
    measured = segment.lane_width
    threshold = settings.clearance_threshold
    low_bound = max(dimensions.width * 0.5, 0.2)
    high_bound = max(measured * 2.5, measured + 4.0)

    def centred(width: float) -> float:
        area = context.area_with_width(segment.lane_id or "", width)
        return _segment_clearance(poses, segment, dimensions, area, settings)

    def offset_best(width: float) -> float:
        area = context.area_with_width(segment.lane_id or "", width)
        return best_clearance(
            poses, segment, dimensions, area, settings, search_radius=search_radius
        )

    def limit(evaluate: Callable[[float], float]) -> float | None:
        if evaluate(high_bound) < threshold:
            return None
        if evaluate(low_bound) >= threshold:
            return low_bound
        low, high = low_bound, high_bound
        for _ in range(10):
            middle = (low + high) / 2
            if evaluate(middle) >= threshold:
                high = middle
            else:
                low = middle
        return high

    centred_limit = limit(centred)
    offset_limit = limit(offset_best)
    anchors = [value for value in (measured, centred_limit, offset_limit) if value is not None]
    span = max(0.4, (max(anchors) - min(anchors)) * 0.9)
    return WidthZones(
        lane_id=segment.lane_id,
        lane_name=segment.lane_name or "",
        measured=measured,
        offset_limit=offset_limit,
        centred_limit=centred_limit,
        scale_low=min(anchors) - span * 0.5,
        scale_high=max(anchors) + span * 0.5,
    )


def analyse_clearance(
    poses: Sequence[PosePoint],
    dimensions: VehicleDimensions,
    lanes: Sequence[Lane],
    settings: AnalysisSettings,
    *,
    turn_threshold: float = math.pi / 6,
    sample_labels: Sequence[int] | None = None,
    search_radius: float = SEARCH_RADIUS,
) -> ClearanceAnalysis | None:
    """Profile, rank and solve the offset headroom of one dispatched path."""

    usable = [pose for pose in poses if pose.yaw is not None]
    if len(usable) < 2:
        return None
    context = LaneContext(lanes, settings)
    area = context.area()
    if area.is_empty or area.area <= 0:
        return None

    segments = _attach_lanes(
        split_segments(poses, turn_threshold=turn_threshold, sample_labels=sample_labels),
        poses,
        context,
    )
    if not segments:
        return None

    result = analyze_path(poses, dimensions, area, settings)
    lengths, total = _segment_lengths(poses)
    cumulative = [0.0]
    for length in lengths:
        cumulative.append(cumulative[-1] + length)

    def locate(source_segment: int | None, within: float) -> tuple[float, int | None]:
        if source_segment is None or total <= 0:
            return 0.0, None
        travelled = cumulative[source_segment] + within * lengths[source_segment]
        owner = next(
            (
                segment.index
                for segment in segments
                if segment.start_pose <= source_segment < segment.end_pose
            ),
            None,
        )
        return travelled / total, owner

    profile: list[ProfileSample] = []
    worst_by_segment: dict[int, tuple[float, int, PosePoint, float]] = {}
    for assessment in result.assessments:
        progress, owner = locate(assessment.source_segment, assessment.progress)
        profile.append(ProfileSample(progress, assessment.clearance, owner))
        if owner is None:
            continue
        current = worst_by_segment.get(owner)
        if current is None or assessment.clearance < current[0]:
            worst_by_segment[owner] = (
                assessment.clearance,
                assessment.source_segment or 0,
                assessment.pose,
                progress,
            )

    bands = tuple(
        solve_band(poses, segment, dimensions, area, settings, search_radius=search_radius)
        for segment in segments
    )
    coupled = couple_bands(segments, bands)

    ranked = sorted(worst_by_segment.items(), key=lambda item: item[1][0])
    bottlenecks: list[Bottleneck] = []
    for rank, (segment_index, (clearance, pose_index, pose, progress)) in enumerate(ranked, 1):
        band = next((item for item in bands if item.segment_index == segment_index), None)
        inside = band.contains(0.0) if band else False
        required = band.nearest_edge(0.0) if band else None
        bottlenecks.append(
            Bottleneck(
                rank=rank,
                segment=segments[segment_index],
                clearance=clearance,
                pose_index=pose_index,
                pose=pose,
                progress=progress,
                required_offset=required,
                inside_band=inside,
                band_feasible=band.feasible if band else False,
                feature=constraining_feature(pose, dimensions, area, segments[segment_index].side),
            )
        )

    outside = sum(1 for band in bands if not band.contains(0.0))
    feasible_bands = [band for band in bands if band.feasible]
    narrowest = min(feasible_bands, key=lambda band: band.width) if feasible_bands else None

    recommended: dict[int, float] = {}
    for segment in segments:
        pair = next((item for item in coupled if item.segment_index == segment.index), None)
        band = next((item for item in bands if item.segment_index == segment.index), None)
        if pair is not None and pair.feasible:
            recommended[segment.index] = min(max(0.0, pair.low), pair.high)
        elif band is not None:
            recommended[segment.index] = band.best
    suggested = min(
        (
            _segment_clearance(
                poses, segments[index], dimensions, area, settings, offset=value
            )
            for index, value in recommended.items()
        ),
        default=None,
    )
    breaches = [sample.clearance for sample in profile if sample.clearance < 0]
    return ClearanceAnalysis(
        status=result.status,
        segments=segments,
        profile=tuple(profile),
        bottlenecks=tuple(bottlenecks),
        bands=bands,
        coupled=coupled,
        threshold=settings.clearance_threshold,
        minimum_clearance=result.minimum_clearance,
        deepest_breach=min(breaches) if breaches else None,
        outside_band_segments=outside,
        narrowest_band=narrowest,
        suggested_clearance=suggested,
        analyzed_samples=result.analyzed_samples,
        pose_count=len(poses),
        recommended_offsets=recommended,
    )


@dataclass(frozen=True, slots=True)
class DegreeRange:
    low: float
    high: float
    feasible: bool = True

    @property
    def width(self) -> float:
        return max(0.0, self.high - self.low) if self.feasible else 0.0


@dataclass(frozen=True, slots=True)
class CornerSolution:
    """One corner's best three-degree-of-freedom answer and what each degree bought."""

    corner: FittedCorner
    entry_offset: float
    exit_offset: float
    arc_start_shift: float
    radius: float
    clearance: float
    baseline_clearance: float
    two_degree_clearance: float
    poses: tuple[PosePoint, ...]
    ranges: dict[str, DegreeRange]

    @property
    def third_degree_gain(self) -> float:
        return self.clearance - self.two_degree_clearance


def _corner_clearance(
    corner: FittedCorner,
    dimensions: VehicleDimensions,
    area: BaseGeometry,
    settings: AnalysisSettings,
    values: tuple[float, float, float],
    *,
    entry_length: float,
    exit_length: float,
) -> tuple[float, float | None, tuple[PosePoint, ...]]:
    built = build_corner_poses(
        corner,
        entry_offset=values[0],
        exit_offset=values[1],
        arc_start_shift=values[2],
        entry_length=entry_length,
        exit_length=exit_length,
        yaw_step=settings.yaw_step,
    )
    if built is None:
        return -UNBOUNDED, None, ()
    result = analyze_path(built.poses, dimensions, area, settings)
    if result.minimum_clearance is None:
        return -UNBOUNDED, built.radius, built.poses
    return result.minimum_clearance, built.radius, built.poses


def _descend(
    evaluate: Callable[[tuple[float, float, float]], float],
    axes: tuple[int, ...],
    spans: tuple[float, float, float],
    *,
    sweeps: int,
) -> tuple[list[float], float]:
    """Coordinate-descend the given axes, halving the span on every sweep."""

    best = [0.0, 0.0, 0.0]
    best_score = evaluate((0.0, 0.0, 0.0))
    for sweep in range(sweeps):
        step_count = 12 if sweep == 0 else 8
        for axis in axes:
            span = spans[axis] / (2**sweep)
            centre = best[axis]
            for step in range(step_count + 1):
                candidate = list(best)
                candidate[axis] = centre - span + 2 * span * step / step_count
                score = evaluate((candidate[0], candidate[1], candidate[2]))
                if score > best_score:
                    best_score = score
                    best = candidate
    return best, best_score


def solve_corner(
    corner: FittedCorner,
    dimensions: VehicleDimensions,
    area: BaseGeometry,
    settings: AnalysisSettings,
    *,
    entry_length: float = 4.0,
    exit_length: float = 4.0,
    search_radius: float = SEARCH_RADIUS,
    sweeps: int = 3,
) -> CornerSolution | None:
    """Solve one corner twice — two degrees of freedom, then three — and report the gain."""

    def evaluate(values: tuple[float, float, float]) -> float:
        return _corner_clearance(
            corner,
            dimensions,
            area,
            settings,
            values,
            entry_length=entry_length,
            exit_length=exit_length,
        )[0]

    spans = (search_radius, search_radius, search_radius)
    baseline = evaluate((0.0, 0.0, 0.0))
    _, two_degree = _descend(evaluate, (0, 1), spans, sweeps=sweeps)
    best, best_score = _descend(evaluate, (0, 1, 2), spans, sweeps=sweeps)

    clearance, radius, poses = _corner_clearance(
        corner,
        dimensions,
        area,
        settings,
        (best[0], best[1], best[2]),
        entry_length=entry_length,
        exit_length=exit_length,
    )
    if radius is None:
        return None

    threshold = settings.clearance_threshold
    reachable = best_score >= threshold
    ranges: dict[str, DegreeRange] = {}
    for axis, name in enumerate(("entry_offset", "exit_offset", "arc_start_shift")):
        low = high = best[axis]
        if reachable:
            for direction in (-1, 1):
                edge = best[axis]
                for step in range(1, COARSE_STEPS + 1):
                    candidate = list(best)
                    candidate[axis] = best[axis] + direction * spans[axis] * step / COARSE_STEPS
                    if evaluate((candidate[0], candidate[1], candidate[2])) < threshold:
                        break
                    edge = candidate[axis]
                if direction < 0:
                    low = edge
                else:
                    high = edge
        ranges[name] = DegreeRange(low, high, reachable)

    return CornerSolution(
        corner=corner,
        entry_offset=best[0],
        exit_offset=best[1],
        arc_start_shift=best[2],
        radius=radius,
        clearance=clearance,
        baseline_clearance=baseline,
        two_degree_clearance=two_degree,
        poses=poses,
        ranges=ranges,
    )


def corner_for_segment(
    poses: Sequence[PosePoint], segment: PathSegment
) -> FittedCorner | None:
    """Fit the straight-arc-straight parametrisation behind one turn segment."""

    if segment.role is not SegmentRole.TURN:
        return None
    return fit_corner(poses, start_index=segment.start_pose, end_index=segment.end_pose)
