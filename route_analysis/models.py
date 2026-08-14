"""Validated domain objects shared by API, storage, analysis, and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite


def _positive(value: float, field_name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")


@dataclass(frozen=True, slots=True)
class Point2D:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not isfinite(self.x) or not isfinite(self.y):
            raise ValueError("point coordinates must be finite")


@dataclass(frozen=True, slots=True)
class PosePoint:
    x: float
    y: float
    yaw: float | None

    def __post_init__(self) -> None:
        if not isfinite(self.x) or not isfinite(self.y):
            raise ValueError("pose coordinates must be finite")
        if self.yaw is not None and not isfinite(self.yaw):
            raise ValueError("pose yaw must be finite when present")


@dataclass(frozen=True, slots=True)
class VehicleDimensions:
    width: float
    center_front: float
    center_rear: float

    def __post_init__(self) -> None:
        _positive(self.width, "width")
        _positive(self.center_front, "center_front")
        _positive(self.center_rear, "center_rear")

    @property
    def length(self) -> float:
        return self.center_front + self.center_rear


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    position_step: float = 0.05
    yaw_step: float = 0.02
    clearance_threshold: float = 0.05
    bezier_tolerance: float = 0.02
    miter_limit: float = 4.0

    def __post_init__(self) -> None:
        _positive(self.position_step, "position_step")
        _positive(self.yaw_step, "yaw_step")
        if not isfinite(self.clearance_threshold) or self.clearance_threshold < 0:
            raise ValueError("clearance_threshold must be finite and non-negative")
        _positive(self.bezier_tolerance, "bezier_tolerance")
        _positive(self.miter_limit, "miter_limit")


class SegmentKind(StrEnum):
    LINE = "line"
    ARC = "arc"
    CUBIC = "cubic"


class JoinStyle(StrEnum):
    MITER = "miter"
    ROUND = "round"


@dataclass(slots=True)
class LaneAnchor:
    point: Point2D
    join_override: JoinStyle | None = None


@dataclass(slots=True)
class LaneSegment:
    kind: SegmentKind = SegmentKind.LINE
    control1: Point2D | None = None
    control2: Point2D | None = None
    arc_center: Point2D | None = None
    clockwise: bool | None = None

    def __post_init__(self) -> None:
        if self.kind is SegmentKind.CUBIC and (self.control1 is None or self.control2 is None):
            raise ValueError("cubic segment control points are required")
        if self.kind is SegmentKind.ARC and (
            self.arc_center is None or self.clockwise is None
        ):
            raise ValueError("arc segment center and direction are required")


@dataclass(slots=True)
class Lane:
    id: str
    name: str
    width: float
    anchors: list[LaneAnchor]
    segments: list[LaneSegment]
    enabled: bool = True
    closed: bool = False
    default_join: JoinStyle = JoinStyle.MITER

    def __post_init__(self) -> None:
        _positive(self.width, "lane width")
        if not self.id.strip():
            raise ValueError("lane id must not be empty")
        if len(self.anchors) < 2:
            raise ValueError("lane requires at least two anchors")
        expected_segments = len(self.anchors) if self.closed else len(self.anchors) - 1
        if len(self.segments) != expected_segments:
            raise ValueError(
                f"lane segment count must be {expected_segments} for this anchor configuration"
            )

    @classmethod
    def create(
        cls,
        lane_id: str,
        name: str,
        width: float,
        points: list[Point2D],
        *,
        closed: bool = False,
        default_join: JoinStyle = JoinStyle.MITER,
    ) -> Lane:
        segment_count = len(points) if closed else max(0, len(points) - 1)
        return cls(
            id=lane_id,
            name=name,
            width=width,
            anchors=[LaneAnchor(point) for point in points],
            segments=[LaneSegment() for _ in range(segment_count)],
            closed=closed,
            default_join=default_join,
        )


class ClearanceStatus(StrEnum):
    SAFE = "safe"
    WARNING = "warning"
    OUTSIDE = "outside"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SampleAssessment:
    pose: PosePoint
    source_segment: int | None
    progress: float
    clearance: float
    outside: bool


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    status: ClearanceStatus
    minimum_clearance: float | None = None
    minimum_clearance_pose: PosePoint | None = None
    first_outside: PosePoint | None = None
    outside_samples: int = 0
    analyzed_samples: int = 0
    skipped_segments: int = 0
    missing_yaw_indices: tuple[int, ...] = ()
    assessments: tuple[SampleAssessment, ...] = field(default_factory=tuple)
    position_step: float | None = None
    yaw_step: float | None = None

    @property
    def incomplete(self) -> bool:
        return bool(self.missing_yaw_indices)
