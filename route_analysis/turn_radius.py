"""Whole-turn radius detection and measurement for scheduling poses."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
class TurnRadiusSection:
    start_index: int
    end_index: int
    cumulative_yaw: float
    kind: TurnKind
    side: TurnSide | None = None
    rotation_center: Point2D | None = None
    front_axle_radius: float | None = None
    radii: Mapping[CornerRadiusKind, float] = field(default_factory=dict)
    corners: Mapping[CornerRadiusKind, Point2D] = field(default_factory=dict)
    end_corners: Mapping[CornerRadiusKind, Point2D] = field(default_factory=dict)
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None and self.rotation_center is not None


@dataclass(frozen=True, slots=True)
class TurnRadiusResult:
    turns: tuple[TurnRadiusSection, ...]
    missing_yaw_indices: tuple[int, ...]

    @property
    def incomplete(self) -> bool:
        return bool(self.missing_yaw_indices)


def _validate_positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


def _append_detected(
    turns: list[DetectedTurn], start: int, end: int, cumulative: float, threshold: float
) -> None:
    if end > start and _strictly_exceeds(abs(cumulative), threshold):
        turns.append(DetectedTurn(start, end, cumulative))


def _strictly_exceeds(value: float, threshold: float) -> bool:
    comparison_tolerance = max(1e-12, threshold * 1e-12)
    return value - threshold > comparison_tolerance


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


def cumulative_yaw_between(
    points: Sequence[PosePoint], start_index: int, end_index: int
) -> float:
    """Continuously unwrap yaw between two ordered source samples."""

    if not 0 <= start_index < end_index < len(points):
        raise ValueError("入弯样本必须位于出弯样本之前")
    missing = [
        index
        for index in range(start_index, end_index + 1)
        if points[index].yaw is None
    ]
    if missing:
        joined = "、".join(str(index) for index in missing)
        raise ValueError(f"样本 {joined} 缺少 yaw")
    cumulative = 0.0
    for index in range(start_index, end_index):
        start_yaw = points[index].yaw
        end_yaw = points[index + 1].yaw
        assert start_yaw is not None and end_yaw is not None
        cumulative += shortest_angle_delta(start_yaw, end_yaw)
    return cumulative


def equivalent_rotation_center(
    start: PosePoint,
    end: PosePoint,
    cumulative_yaw: float,
) -> Point2D | None:
    """Solve one fixed center from endpoint positions and unwrapped rotation."""

    if not math.isfinite(cumulative_yaw) or abs(cumulative_yaw) <= 1e-12:
        return None
    chord_x = end.x - start.x
    chord_y = end.y - start.y
    chord = math.hypot(chord_x, chord_y)
    if chord <= 1e-12:
        return None
    tangent = math.tan(cumulative_yaw / 2)
    if not math.isfinite(tangent) or abs(tangent) <= 1e-12:
        return None
    midpoint_x = (start.x + end.x) / 2
    midpoint_y = (start.y + end.y) / 2
    normal_x = -chord_y / chord
    normal_y = chord_x / chord
    offset = chord / (2 * tangent)
    x = midpoint_x + normal_x * offset
    y = midpoint_y + normal_y * offset
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return Point2D(0.0 if abs(x) <= 1e-12 else x, 0.0 if abs(y) <= 1e-12 else y)


def _world_corner(pose: PosePoint, longitudinal: float, lateral: float) -> Point2D:
    if pose.yaw is None:
        raise ValueError("corner radius requires a pose with yaw")
    cosine = math.cos(pose.yaw)
    sine = math.sin(pose.yaw)
    return Point2D(
        pose.x + longitudinal * cosine - lateral * sine,
        pose.y + longitudinal * sine + lateral * cosine,
    )


def _corner_points(
    pose: PosePoint,
    dimensions: VehicleDimensions,
    side: TurnSide,
) -> dict[CornerRadiusKind, Point2D]:
    half_width = dimensions.width / 2
    inner_lateral = half_width if side is TurnSide.LEFT else -half_width
    outer_lateral = -inner_lateral
    return {
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


def _invalid_section(
    start_index: int,
    end_index: int,
    cumulative_yaw: float,
    error: str,
) -> TurnRadiusSection:
    kind = (
        TurnKind.UTURN
        if _strictly_exceeds(abs(cumulative_yaw), 0.75 * math.pi)
        else TurnKind.TURN
    )
    return TurnRadiusSection(start_index, end_index, cumulative_yaw, kind, error=error)


def calculate_turn_radius(
    points: Sequence[PosePoint],
    dimensions: VehicleDimensions,
    *,
    start_index: int,
    end_index: int,
    cumulative_yaw: float | None = None,
) -> TurnRadiusSection:
    """Calculate one whole-turn front-axle and four-corner radius measurement."""

    try:
        yaw_change = (
            cumulative_yaw_between(points, start_index, end_index)
            if cumulative_yaw is None
            else cumulative_yaw
        )
    except ValueError as exc:
        return _invalid_section(start_index, end_index, 0.0, str(exc))
    start = points[start_index]
    end = points[end_index]
    if start.yaw is None or end.yaw is None:
        return _invalid_section(start_index, end_index, yaw_change, "转弯端点缺少 yaw")
    if abs(yaw_change) <= 1e-12:
        return _invalid_section(start_index, end_index, yaw_change, "累计 yaw 变化为零")
    center = equivalent_rotation_center(start, end, yaw_change)
    if center is None:
        return _invalid_section(start_index, end_index, yaw_change, "无法求得有限整弯等效旋转中心")

    heading_x = math.cos(start.yaw)
    heading_y = math.sin(start.yaw)
    cross = heading_x * (center.y - start.y) - heading_y * (center.x - start.x)
    if abs(cross) <= 1e-12:
        return _invalid_section(start_index, end_index, yaw_change, "旋转中心不在车体左右侧")
    side = TurnSide.LEFT if cross > 0 else TurnSide.RIGHT
    corners = _corner_points(start, dimensions, side)
    end_corners = _corner_points(end, dimensions, side)
    radii = {
        kind: math.hypot(corner.x - center.x, corner.y - center.y)
        for kind, corner in corners.items()
    }
    front_axle_radius = math.hypot(start.x - center.x, start.y - center.y)
    values = (front_axle_radius, *radii.values())
    if not all(math.isfinite(value) for value in values):
        return _invalid_section(start_index, end_index, yaw_change, "转弯半径不是有限数")
    kind = (
        TurnKind.UTURN
        if _strictly_exceeds(abs(yaw_change), 0.75 * math.pi)
        else TurnKind.TURN
    )
    return TurnRadiusSection(
        start_index,
        end_index,
        yaw_change,
        kind,
        side,
        center,
        front_axle_radius,
        radii,
        corners,
        end_corners,
    )


def analyze_turn_radii(
    points: Sequence[PosePoint],
    dimensions: VehicleDimensions,
    *,
    threshold: float = math.pi / 6,
) -> TurnRadiusResult:
    """Detect and calculate one whole-turn measurement per automatic turn."""

    _validate_positive(threshold, "turn threshold")
    turns = tuple(
        calculate_turn_radius(
            points,
            dimensions,
            start_index=detected.start_index,
            end_index=detected.end_index,
            cumulative_yaw=detected.cumulative_yaw,
        )
        for detected in detect_turns(points, threshold=threshold)
    )
    return TurnRadiusResult(
        turns,
        tuple(index for index, point in enumerate(points) if point.yaw is None),
    )
