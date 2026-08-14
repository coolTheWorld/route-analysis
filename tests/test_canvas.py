import math

import pytest
from pytestqt.qtbot import QtBot

from route_analysis.canvas import RouteCanvas
from route_analysis.lane_generation import BendMode, generate_lane
from route_analysis.models import (
    JoinStyle,
    Lane,
    Point2D,
    PosePoint,
    SegmentKind,
    VehicleDimensions,
)
from route_analysis.storage import LaneLayout


def layout() -> LaneLayout:
    return LaneLayout(
        "aaaaaaaaaaaaaaaa",
        "42",
        [Lane.create("lane-1", "主车道", 2, [Point2D(0, 0), Point2D(3, 0)])],
    )


def test_map_direction_rotates_display_but_keeps_raw_lane_coordinates(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.load_layout(layout())

    canvas.set_map_direction(math.pi / 2)
    displayed = canvas.to_display(Point2D(1, 0))
    recovered = canvas.to_raw(displayed)

    assert displayed.x == pytest.approx(0, abs=1e-9)
    assert displayed.y == pytest.approx(1)
    assert recovered == Point2D(1, 0)
    assert canvas.current_layout().lanes[0].anchors[1].point == Point2D(3, 0)


def test_lane_edits_are_undoable_and_new_lanes_default_to_sharp(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.load_layout(layout())

    lane_id = canvas.add_lane([Point2D(0, 2), Point2D(3, 2)], width=2.5, name="次车道")
    assert canvas.current_layout().lanes[-1].default_join is JoinStyle.MITER

    canvas.set_lane_width(lane_id, 3.0)
    assert canvas.current_layout().lanes[-1].width == 3.0
    canvas.undo_stack.undo()
    assert canvas.current_layout().lanes[-1].width == 2.5
    canvas.undo_stack.redo()
    assert canvas.current_layout().lanes[-1].width == 3.0


def test_segment_and_anchor_properties_can_be_edited(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.load_layout(layout())

    canvas.set_segment_kind("lane-1", 0, SegmentKind.CUBIC)
    lane = canvas.current_layout().lanes[0]
    assert lane.segments[0].control1 == Point2D(1, 0)
    assert lane.segments[0].control2 == Point2D(2, 0)

    canvas.set_control_point("lane-1", 0, 1, Point2D(1, 1))
    canvas.set_anchor_join("lane-1", 1, JoinStyle.ROUND)
    canvas.set_anchor_position("lane-1", 1, Point2D(4, 1))
    lane = canvas.current_layout().lanes[0]
    assert lane.segments[0].control1 == Point2D(1, 1)
    assert lane.anchors[1].join_override is JoinStyle.ROUND
    assert lane.anchors[1].point == Point2D(4, 1)


def test_paths_show_center_points_when_yaw_is_missing(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.set_paths(
        (PosePoint(0, 0, 0), PosePoint(1, 0, None)),
        (PosePoint(0, 1, 0.1),),
        VehicleDimensions(1, 1, 1),
    )

    assert canvas.path_point_counts == {"dispatched": 2, "actual": 1}
    assert canvas.missing_yaw_counts == {"dispatched": 1, "actual": 0}
    assert len(canvas.scene().items()) > 3


def test_fit_content_keeps_y_up_and_brings_far_coordinates_into_view(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_paths(
        (PosePoint(1000, 2000, 0), PosePoint(1010, 2005, 0.2)),
        (),
        VehicleDimensions(1, 1, 1),
    )

    canvas.fit_content()

    assert canvas.transform().m11() > 0
    assert canvas.transform().m22() < 0
    displayed = canvas.to_display(Point2D(1005, 2002.5))
    viewport_point = canvas.mapFromScene(displayed.x, displayed.y)
    assert canvas.viewport().rect().contains(viewport_point)


def test_generated_lane_preview_is_non_mutating_and_confirm_is_one_undo_step(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.load_layout(layout())
    generated = generate_lane(
        [Point2D(0, 2), Point2D(1, 2.1), Point2D(2, 2)],
        lane_id="generated",
        name="自动车道",
        width=2,
        mode=BendMode.BEZIER,
        maximum_deviation=0.05,
    )
    initial_items = len(canvas.scene().items())

    canvas.set_lane_preview(generated.lane)

    assert len(canvas.current_layout().lanes) == 1
    assert canvas.undo_stack.count() == 0
    assert len(canvas.scene().items()) > initial_items

    canvas.add_generated_lane(generated.lane)

    assert [lane.name for lane in canvas.current_layout().lanes] == ["主车道", "自动车道"]
    assert canvas.undo_stack.count() == 1
    canvas.undo_stack.undo()
    assert [lane.name for lane in canvas.current_layout().lanes] == ["主车道"]


def test_generated_arc_radius_edit_is_undoable(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    generated = generate_lane(
        [
            Point2D(-2, 0),
            Point2D(-1, 0),
            Point2D(-1 + math.sqrt(0.5), 1 - math.sqrt(0.5)),
            Point2D(0, 1),
            Point2D(0, 2),
        ],
        lane_id="arc-lane",
        name="圆弧车道",
        width=2,
        mode=BendMode.ROUND,
        maximum_deviation=0.08,
    )
    canvas.load_layout(LaneLayout("aaaaaaaaaaaaaaaa", "42", [generated.lane]))
    arc_index = next(
        index
        for index, segment in enumerate(generated.lane.segments)
        if segment.kind is SegmentKind.ARC
    )

    canvas.set_arc_radius("arc-lane", arc_index, 0.5)

    edited = canvas.current_layout().lanes[0]
    assert edited.segments[arc_index].arc_center is not None
    canvas.undo_stack.undo()
    restored = canvas.current_layout().lanes[0]
    assert restored.anchors[arc_index].point == generated.lane.anchors[arc_index].point
