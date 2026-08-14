import pytest

from route_analysis.models import (
    AnalysisSettings,
    JoinStyle,
    Lane,
    LaneAnchor,
    LaneSegment,
    Point2D,
    SegmentKind,
    VehicleDimensions,
)


@pytest.mark.parametrize(
    ("width", "front", "rear"),
    [(0.0, 1.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, -0.1)],
)
def test_vehicle_dimensions_must_be_positive(width: float, front: float, rear: float) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        VehicleDimensions(width=width, center_front=front, center_rear=rear)


def test_analysis_settings_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        AnalysisSettings(position_step=0.0)


def test_lane_validates_segment_count_and_bezier_controls() -> None:
    anchors = [LaneAnchor(Point2D(0, 0)), LaneAnchor(Point2D(2, 0))]
    with pytest.raises(ValueError, match="segment count"):
        Lane(id="lane", name="Lane", width=2, anchors=anchors, segments=[])

    with pytest.raises(ValueError, match="control points"):
        Lane(
            id="lane",
            name="Lane",
            width=2,
            anchors=anchors,
            segments=[LaneSegment(kind=SegmentKind.CUBIC)],
        )


def test_new_lane_defaults_to_sharp_join() -> None:
    lane = Lane.create("lane", "Lane", width=2.0, points=[Point2D(0, 0), Point2D(1, 0)])

    assert lane.default_join is JoinStyle.MITER
    assert lane.segments[0].kind is SegmentKind.LINE


def test_arc_segment_requires_center_and_direction() -> None:
    center = Point2D(0, 0)

    arc = LaneSegment(
        kind=SegmentKind.ARC,
        arc_center=center,
        clockwise=False,
    )

    assert arc.arc_center == center
    assert arc.clockwise is False
    with pytest.raises(ValueError, match="arc segment center and direction"):
        LaneSegment(kind=SegmentKind.ARC)
