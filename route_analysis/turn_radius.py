"""Turn detection and vehicle-corner radius analysis for scheduling poses."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from route_analysis.geometry import shortest_angle_delta
from route_analysis.models import Point2D, PosePoint, VehicleDimensions


class TurnSide(StrEnum):
    LEFT = "left"
    RIGHT = "right"


class TurnKind(StrEnum):
    TURN = "turn"
    UTURN = "uturn"


class CornerRadiusKind(StrEnum):
    FRONT_OUTER = "front_outer"
    REAR_OUTER = "rear_outer"
    FRONT_INNER = "front_inner"
    REAR_INNER = "rear_inner"


@dataclass(frozen=True, slots=True)
class DetectedTurn:
    start_index: int
    end_index: int
    cumulative_yaw: float


@dataclass(frozen=True, slots=True)
class RadiusObservation:
    pose_index: int
    pose: PosePoint
    rotation_center: Point2D
    side: TurnSide
    radii: Mapping[CornerRadiusKind, float]


@dataclass(frozen=True, slots=True)
class RadiusStatistics:
    minimum: float
    median: float
    maximum: float
    maximum_observation: RadiusObservation


@dataclass(frozen=True, slots=True)
class TurnRadiusSection:
    start_index: int
    end_index: int
    cumulative_yaw: float
    side: TurnSide
    kind: TurnKind
    observations: tuple[RadiusObservation, ...]
    statistics: Mapping[CornerRadiusKind, RadiusStatistics]


@dataclass(frozen=True, slots=True)
class TurnRadiusResult:
    turns: tuple[TurnRadiusSection, ...]
    overall: Mapping[CornerRadiusKind, RadiusStatistics]
    missing_yaw_indices: tuple[int, ...]
    skipped_icr_samples: int

    @property
    def incomplete(self) -> bool:
        return bool(self.missing_yaw_indices)


def _validate_positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


def _append_detected(
    turns: list[DetectedTurn], start: int, end: int, cumulative: float, threshold: float
) -> None:
    comparison_tolerance = max(1e-12, threshold * 1e-12)
    if end > start and abs(cumulative) - threshold > comparison_tolerance:
        turns.append(DetectedTurn(start, end, cumulative))


def _detect_valid_block(
    points: Sequence[PosePoint], block_start: int, block_end: int, threshold: float
) -> list[DetectedTurn]:
    turns: list[DetectedTurn] = []
    direction = 0
    section_start = block_start
    accumulated = 0.0
    reverse_accumulated = 0.0
    reverse_start: int | None = None
    before_reverse = 0.0

    for index in range(block_start, block_end):
        start_yaw = points[index].yaw
        end_yaw = points[index + 1].yaw
        if start_yaw is None or end_yaw is None:
            continue
        delta = shortest_angle_delta(start_yaw, end_yaw)
        if abs(delta) <= 1e-15:
            continue
        sign = 1 if delta > 0 else -1
        magnitude = abs(delta)
        if direction == 0:
            direction = sign
            section_start = index
            accumulated = magnitude
            continue
        if sign == direction:
            accumulated += magnitude
            reverse_accumulated = 0.0
            reverse_start = None
            continue

        if reverse_start is None:
            reverse_start = index
            before_reverse = accumulated
        reverse_accumulated += magnitude
        accumulated -= magnitude
        if reverse_accumulated >= threshold:
            _append_detected(
                turns,
                section_start,
                reverse_start,
                direction * before_reverse,
                threshold,
            )
            direction = sign
            section_start = reverse_start
            accumulated = reverse_accumulated
            reverse_accumulated = 0.0
            reverse_start = None

    _append_detected(
        turns,
        section_start,
        block_end,
        direction * max(0.0, accumulated),
        threshold,
    )
    return turns


def detect_turns(
    points: Sequence[PosePoint], *, threshold: float = math.pi / 6
) -> tuple[DetectedTurn, ...]:
    """Detect continuous turn sections using strictly-above cumulative yaw."""

    _validate_positive(threshold, "turn threshold")
    turns: list[DetectedTurn] = []
    index = 0
    while index < len(points):
        while index < len(points) and points[index].yaw is None:
            index += 1
        block_start = index
        while index < len(points) and points[index].yaw is not None:
            index += 1
        block_end = index - 1
        if block_end > block_start:
            turns.extend(_detect_valid_block(points, block_start, block_end, threshold))
    return tuple(turns)


def _cumulative_distances(
    points: Sequence[PosePoint], start_index: int, end_index: int
) -> list[float]:
    distances = [0.0]
    for index in range(start_index, end_index):
        first = points[index]
        second = points[index + 1]
        distances.append(distances[-1] + math.hypot(second.x - first.x, second.y - first.y))
    return distances


def _window_indices(
    distances: Sequence[float], local_index: int, window: float
) -> tuple[int, int]:
    half_window = window / 2
    center = distances[local_index]
    lower_target = center - half_window
    upper_target = center + half_window
    lower = local_index
    while lower > 0 and distances[lower] > lower_target:
        lower -= 1
    upper = local_index
    while upper < len(distances) - 1 and distances[upper] < upper_target:
        upper += 1
    if lower == upper:
        if upper < len(distances) - 1:
            upper += 1
        elif lower > 0:
            lower -= 1
    return lower, upper


def instantaneous_rotation_center(start: PosePoint, end: PosePoint) -> Point2D | None:
    """Compute a planar instantaneous rotation center from two valid poses."""

    if start.yaw is None or end.yaw is None:
        return None
    yaw_delta = shortest_angle_delta(start.yaw, end.yaw)
    if abs(yaw_delta) <= 1e-12:
        return None
    chord_x = end.x - start.x
    chord_y = end.y - start.y
    chord = math.hypot(chord_x, chord_y)
    if chord <= 1e-12:
        return None
    tangent = math.tan(yaw_delta / 2)
    if abs(tangent) <= 1e-12:
        return None
    midpoint_x = (start.x + end.x) / 2
    midpoint_y = (start.y + end.y) / 2
    normal_x = -chord_y / chord
    normal_y = chord_x / chord
    offset = chord / (2 * tangent)
    center = Point2D(midpoint_x + normal_x * offset, midpoint_y + normal_y * offset)
    if not math.isfinite(center.x) or not math.isfinite(center.y):
        return None
    return center


def _world_corner(pose: PosePoint, longitudinal: float, lateral: float) -> Point2D:
    if pose.yaw is None:
        raise ValueError("corner radius requires a pose with yaw")
    cosine = math.cos(pose.yaw)
    sine = math.sin(pose.yaw)
    return Point2D(
        pose.x + longitudinal * cosine - lateral * sine,
        pose.y + longitudinal * sine + lateral * cosine,
    )


def _corner_radii(
    pose: PosePoint, center: Point2D, dimensions: VehicleDimensions
) -> tuple[TurnSide, dict[CornerRadiusKind, float]]:
    if pose.yaw is None:
        raise ValueError("corner radius requires a pose with yaw")
    heading_x = math.cos(pose.yaw)
    heading_y = math.sin(pose.yaw)
    center_offset_x = center.x - pose.x
    center_offset_y = center.y - pose.y
    cross = heading_x * center_offset_y - heading_y * center_offset_x
    side = TurnSide.LEFT if cross > 0 else TurnSide.RIGHT
    half_width = dimensions.width / 2
    inner_lateral = half_width if side is TurnSide.LEFT else -half_width
    outer_lateral = -inner_lateral
    corners = {
        CornerRadiusKind.FRONT_OUTER: _world_corner(
            pose, dimensions.center_front, outer_lateral
        ),
        CornerRadiusKind.REAR_OUTER: _world_corner(
            pose, -dimensions.center_rear, outer_lateral
        ),
        CornerRadiusKind.FRONT_INNER: _world_corner(
            pose, dimensions.center_front, inner_lateral
        ),
        CornerRadiusKind.REAR_INNER: _world_corner(
            pose, -dimensions.center_rear, inner_lateral
        ),
    }
    radii = {
        kind: math.hypot(corner.x - center.x, corner.y - center.y)
        for kind, corner in corners.items()
    }
    return side, radii


def _statistics(
    observations: Sequence[RadiusObservation], kind: CornerRadiusKind
) -> RadiusStatistics:
    maximum_observation = max(observations, key=lambda observation: observation.radii[kind])
    values = [observation.radii[kind] for observation in observations]
    return RadiusStatistics(
        minimum=min(values),
        median=statistics.median(values),
        maximum=maximum_observation.radii[kind],
        maximum_observation=maximum_observation,
    )


def _summaries(
    observations: Sequence[RadiusObservation],
) -> dict[CornerRadiusKind, RadiusStatistics]:
    if not observations:
        return {}
    return {kind: _statistics(observations, kind) for kind in CornerRadiusKind}


def analyze_turn_radii(
    points: Sequence[PosePoint],
    dimensions: VehicleDimensions,
    *,
    threshold: float = math.pi / 6,
    distance_window: float = 0.5,
) -> TurnRadiusResult:
    """Analyze all detected turns and four vehicle-corner radius distributions."""

    _validate_positive(threshold, "turn threshold")
    _validate_positive(distance_window, "radius distance window")
    detected = detect_turns(points, threshold=threshold)
    sections: list[TurnRadiusSection] = []
    all_observations: list[RadiusObservation] = []
    skipped = 0
    for turn in detected:
        distances = _cumulative_distances(points, turn.start_index, turn.end_index)
        observations: list[RadiusObservation] = []
        for pose_index in range(turn.start_index, turn.end_index + 1):
            local_index = pose_index - turn.start_index
            lower, upper = _window_indices(distances, local_index, distance_window)
            center = instantaneous_rotation_center(
                points[turn.start_index + lower], points[turn.start_index + upper]
            )
            pose = points[pose_index]
            if center is None or pose.yaw is None:
                skipped += 1
                continue
            side, radii = _corner_radii(pose, center, dimensions)
            if not all(math.isfinite(value) for value in radii.values()):
                skipped += 1
                continue
            observations.append(RadiusObservation(pose_index, pose, center, side, radii))
        if not observations:
            continue
        side_counts = Counter(observation.side for observation in observations)
        side = side_counts.most_common(1)[0][0]
        kind = TurnKind.UTURN if abs(turn.cumulative_yaw) > 0.75 * math.pi else TurnKind.TURN
        section = TurnRadiusSection(
            turn.start_index,
            turn.end_index,
            turn.cumulative_yaw,
            side,
            kind,
            tuple(observations),
            _summaries(observations),
        )
        sections.append(section)
        all_observations.extend(observations)
    return TurnRadiusResult(
        tuple(sections),
        _summaries(all_observations),
        tuple(index for index, point in enumerate(points) if point.yaw is None),
        skipped,
    )
