import math

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsTextItem,
)
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
from route_analysis.turn_radius import calculate_turn_radius


def layout() -> LaneLayout:
    return LaneLayout(
        "aaaaaaaaaaaaaaaa",
        "42",
        [Lane.create("lane-1", "主车道", 2, [Point2D(0, 0), Point2D(3, 0)])],
    )


def _draft_path(canvas: RouteCanvas, tag: str) -> QGraphicsPathItem:
    return next(
        item
        for item in canvas.scene().items()
        if isinstance(item, QGraphicsPathItem) and item.data(0) == tag
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


def test_lane_drawing_previews_snapped_candidate_before_first_anchor(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_paths(
        (PosePoint(2, 1, 0),),
        (),
        VehicleDimensions(1, 1, 1),
    )
    canvas.start_lane_drawing(width=2)
    route_item = next(
        item
        for item in canvas.scene().items()
        if isinstance(item, QGraphicsPathItem) and item.zValue() == 5
    )

    qtbot.mouseMove(canvas.viewport(), pos=canvas.mapFromScene(2.03, 1.01))

    assert canvas.draft_points == ()
    assert canvas.draft_hover_point == Point2D(2, 1)
    assert any(item.data(0) == "draft-candidate" for item in canvas.scene().items())
    assert not any(item.data(0) == "draft-centerline" for item in canvas.scene().items())
    assert route_item in canvas.scene().items()


def test_lane_drawing_previews_centerline_and_width_without_mutating_layout(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_snap_enabled(False)
    canvas.start_lane_drawing(width=2)

    qtbot.mouseClick(
        canvas.viewport(),
        Qt.MouseButton.LeftButton,
        pos=canvas.mapFromScene(0, 0),
    )
    qtbot.mouseMove(canvas.viewport(), pos=canvas.mapFromScene(3, 0))

    assert canvas.draft_points == (Point2D(0, 0),)
    assert canvas.draft_hover_point is not None
    assert canvas.draft_hover_point.x == pytest.approx(3, abs=0.05)
    assert canvas.draft_hover_point.y == pytest.approx(0, abs=0.05)
    assert canvas.current_layout().lanes == []
    assert _draft_path(canvas, "draft-centerline").path().elementCount() == 2
    assert _draft_path(canvas, "draft-area").path().boundingRect().height() == pytest.approx(2)

    canvas.set_draft_lane_width(4)

    assert canvas.draft_width == 4
    assert _draft_path(canvas, "draft-area").path().boundingRect().height() == pytest.approx(4)
    assert canvas.current_layout().lanes == []


def test_lane_drawing_fixes_each_clicked_anchor_and_commits_only_fixed_points(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_snap_enabled(False)
    canvas.start_lane_drawing(width=2)
    first = canvas.mapFromScene(0, 0)
    second = canvas.mapFromScene(3, 0)
    hover = canvas.mapFromScene(3, 2)

    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=first)
    qtbot.mouseMove(canvas.viewport(), pos=second)
    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=second)
    qtbot.mouseMove(canvas.viewport(), pos=hover)

    assert len(canvas.draft_points) == 2
    assert canvas.current_layout().lanes == []

    lane_id = canvas.finish_lane_drawing()

    assert lane_id is not None
    lane = canvas.current_layout().lanes[0]
    assert len(lane.anchors) == 2
    assert lane.anchors[0].point.x == pytest.approx(0, abs=0.05)
    assert lane.anchors[1].point.x == pytest.approx(3, abs=0.05)
    assert canvas.undo_stack.count() == 1


def test_lane_drawing_requires_two_points_and_rejects_duplicate_anchor(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_snap_enabled(False)
    canvas.start_lane_drawing(width=2)
    point = canvas.mapFromScene(0, 0)
    qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)

    with qtbot.waitSignal(canvas.drawing_status_changed, timeout=1000) as duplicate:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert "重合" in duplicate.args[0]
    assert len(canvas.draft_points) == 1

    with qtbot.waitSignal(canvas.drawing_status_changed, timeout=1000) as incomplete:
        result = canvas.finish_lane_drawing()
    assert result is None
    assert "至少需要两个锚点" in incomplete.args[0]
    assert canvas.is_drawing is True


def test_lane_drawing_backspace_leave_and_escape_manage_only_draft_state(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_snap_enabled(False)
    canvas.start_lane_drawing(width=2)
    for x in (0, 2, 4):
        qtbot.mouseClick(
            canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=canvas.mapFromScene(x, 0),
        )
    qtbot.mouseMove(canvas.viewport(), pos=canvas.mapFromScene(4, 2))

    qtbot.keyClick(canvas, Qt.Key.Key_Backspace)

    assert len(canvas.draft_points) == 2
    assert _draft_path(canvas, "draft-centerline").path().elementCount() == 3

    QApplication.sendEvent(canvas, QEvent(QEvent.Type.Leave))

    assert canvas.draft_hover_point is None
    assert _draft_path(canvas, "draft-centerline").path().elementCount() == 2
    assert not any(item.data(0) == "draft-candidate" for item in canvas.scene().items())

    qtbot.keyClick(canvas, Qt.Key.Key_Escape)

    assert canvas.is_drawing is False
    assert canvas.draft_points == ()
    assert canvas.current_layout().lanes == []


def test_lane_drawing_double_click_finishes_without_duplicate_anchor(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_snap_enabled(False)
    canvas.start_lane_drawing(width=2)
    qtbot.mouseClick(
        canvas.viewport(),
        Qt.MouseButton.LeftButton,
        pos=canvas.mapFromScene(0, 0),
    )

    qtbot.mouseDClick(
        canvas.viewport(),
        Qt.MouseButton.LeftButton,
        pos=canvas.mapFromScene(3, 0),
    )

    assert canvas.is_drawing is False
    lane = canvas.current_layout().lanes[0]
    assert len(lane.anchors) == 2
    assert lane.anchors[1].point.x == pytest.approx(3, abs=0.05)


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


def test_lane_length_scaling_is_one_undoable_edit(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.load_layout(layout())

    canvas.set_lane_length("lane-1", 6)

    scaled = canvas.current_layout().lanes[0]
    assert [anchor.point for anchor in scaled.anchors] == [Point2D(-1.5, 0), Point2D(4.5, 0)]
    assert scaled.width == 2
    assert canvas.undo_stack.count() == 1
    canvas.undo_stack.undo()
    assert [anchor.point for anchor in canvas.current_layout().lanes[0].anchors] == [
        Point2D(0, 0),
        Point2D(3, 0),
    ]


def test_whole_lane_hit_prefers_selected_then_topmost_and_allows_disabled(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    first = Lane.create("first", "First", 2, [Point2D(0, 0), Point2D(3, 0)])
    first.enabled = False
    second = Lane.create("second", "Second", 2, [Point2D(0, 0), Point2D(3, 0)])
    canvas.load_layout(LaneLayout("aaaaaaaaaaaaaaaa", "42", [first, second]))

    canvas.select_lane("first")
    selected_target = canvas._hit_test(Point2D(1.5, 0))
    canvas.select_lane(None)
    topmost_target = canvas._hit_test(Point2D(1.5, 0))

    assert selected_target is not None
    assert selected_target.kind == "lane"
    assert selected_target.lane_id == "first"
    assert topmost_target is not None
    assert topmost_target.kind == "lane"
    assert topmost_target.lane_id == "second"


def test_whole_lane_mouse_drag_moves_all_geometry_as_one_undo_step(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.load_layout(layout())
    canvas.set_snap_enabled(False)
    start = canvas.mapFromScene(1.5, 0)
    end = canvas.mapFromScene(3.5, 2)

    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=end)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)

    moved = canvas.current_layout().lanes[0]
    assert moved.anchors[0].point.x == pytest.approx(2, abs=0.05)
    assert moved.anchors[0].point.y == pytest.approx(2, abs=0.05)
    assert moved.anchors[1].point.x == pytest.approx(5, abs=0.05)
    assert moved.anchors[1].point.y == pytest.approx(2, abs=0.05)
    assert canvas.undo_stack.count() == 1
    canvas.undo_stack.undo()
    assert canvas.current_layout().lanes[0].anchors[0].point == Point2D(0, 0)


def test_selected_whole_turn_draws_five_labeled_radius_trajectories(qtbot: QtBot) -> None:
    dimensions = VehicleDimensions(2, 3, 1)
    path = tuple(
        PosePoint(5 * math.cos(angle), 5 * math.sin(angle), angle + math.pi / 2)
        for angle in (index * math.pi / 40 for index in range(21))
    )
    measurement = calculate_turn_radius(
        path,
        dimensions,
        start_index=0,
        end_index=20,
    )
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.set_paths(path, (), dimensions)
    items_before = len(canvas.scene().items())

    canvas.show_turn_radius_observation("dispatched", measurement)

    texts = [
        item.toPlainText()
        for item in canvas.scene().items()
        if isinstance(item, QGraphicsTextItem)
    ]
    assert len(canvas.scene().items()) > items_before
    assert len([text for text in texts if text.endswith(" m")]) == 5
    assert any(text.startswith("前轴中心") for text in texts)


def test_manual_radius_mode_highlights_suggestions_selects_path_points_and_escapes(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    path = (PosePoint(0, 0, 0), PosePoint(1, 0, 0.4), PosePoint(2, 0, 0.8))
    canvas.set_paths(path, (), VehicleDimensions(1, 1, 1))
    items_before = len(canvas.scene().items())

    canvas.set_manual_radius_mode("dispatched", {0, 2})

    assert len(canvas.scene().items()) > items_before
    point = canvas.mapFromScene(1, 0)
    with qtbot.waitSignal(canvas.radius_endpoint_selected, timeout=1000) as selected:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert selected.args == ["dispatched", 1]

    with qtbot.waitSignal(canvas.manual_radius_cancelled, timeout=1000) as cancelled:
        qtbot.keyClick(canvas, Qt.Key.Key_Escape)
    assert cancelled.args == ["dispatched"]


def test_selected_path_point_highlight_is_independent_of_normal_path_layers(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.set_paths(
        (PosePoint(0, 0, 0), PosePoint(2, 0, 0.5)),
        (),
        VehicleDimensions(1, 2, 1),
    )
    canvas.set_path_layer("dispatched", centerline=False, vehicles=False)

    selected = canvas.select_path_point("dispatched", 1)

    assert selected is True
    assert canvas.selected_path_point == ("dispatched", 1)
    assert any(
        isinstance(item, QGraphicsEllipseItem) and item.zValue() == 55
        for item in canvas.scene().items()
    )
    assert any(
        isinstance(item, QGraphicsPolygonItem) and item.zValue() == 54
        for item in canvas.scene().items()
    )


def test_selected_missing_yaw_point_only_draws_center_marker(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.set_paths((PosePoint(1, 2, None),), (), VehicleDimensions(1, 2, 1))

    canvas.select_path_point("dispatched", 0)

    assert any(
        isinstance(item, QGraphicsEllipseItem) and item.zValue() == 55
        for item in canvas.scene().items()
    )
    assert not any(
        isinstance(item, QGraphicsPolygonItem) and item.zValue() == 54
        for item in canvas.scene().items()
    )


def test_selecting_offscreen_point_centers_without_changing_zoom(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_paths((PosePoint(0, 0, 0),), (), VehicleDimensions(1, 1, 1))
    canvas.centerOn(100, 100)
    scale_before = (canvas.transform().m11(), canvas.transform().m22())

    canvas.select_path_point("dispatched", 0)

    viewport_point = canvas.mapFromScene(0, 0)
    assert canvas.viewport().rect().contains(viewport_point)
    assert (canvas.transform().m11(), canvas.transform().m22()) == scale_before


def test_map_click_uses_active_path_and_cycles_overlapping_source_indices(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.set_paths(
        (PosePoint(0, 0, 0), PosePoint(0, 0, 0.2)),
        (PosePoint(0, 0, 0.4),),
        VehicleDimensions(1, 1, 1),
        source_indices={"dispatched": (2, 5), "actual": (9,)},
    )
    point = canvas.mapFromScene(0, 0)

    with qtbot.waitSignal(canvas.path_point_selected, timeout=1000) as first:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    with qtbot.waitSignal(canvas.path_point_selected, timeout=1000) as second:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)

    assert first.args == ["dispatched", 2]
    assert second.args == ["dispatched", 5]

    canvas.set_active_path("actual")
    with qtbot.waitSignal(canvas.path_point_selected, timeout=1000) as actual:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert actual.args == ["actual", 9]


def test_dragging_lane_over_path_moves_lane_instead_of_selecting_point(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.load_layout(layout())
    canvas.set_snap_enabled(False)
    canvas.set_paths((PosePoint(1.5, 0, 0),), (), VehicleDimensions(1, 1, 1))
    start = canvas.mapFromScene(1.5, 0)
    end = canvas.mapFromScene(3.5, 2)

    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=end)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)

    moved = canvas.current_layout().lanes[0]
    assert moved.anchors[0].point.x == pytest.approx(2, abs=0.05)
    assert moved.anchors[0].point.y == pytest.approx(2, abs=0.05)
    assert canvas.selected_path_point is None
