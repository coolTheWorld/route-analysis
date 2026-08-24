import math

import pytest
from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsTextItem,
    QMenu,
    QStyleOptionGraphicsItem,
)
from pytestqt.qtbot import QtBot

from route_analysis.canvas import (
    PATH_POINT_CROWDING_FACTOR,
    PathPointsItem,
    RouteCanvas,
    marker_diameters,
    neighbourhood_indices,
    radius_candidate_indices,
)
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


def _path_points_items(canvas: RouteCanvas) -> list[QGraphicsItem]:
    return [item for item in canvas.scene().items() if item.data(0) == "path-points"]


def _path_points_item(canvas: RouteCanvas, path_name: str) -> PathPointsItem:
    item = next(item for item in _path_points_items(canvas) if item.data(1) == path_name)
    assert isinstance(item, PathPointsItem)
    return item


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


def _drive_pick_menu(position: int) -> None:
    """Choose one entry of the modal endpoint menu once it opens, or close it."""

    def act(attempt: int = 0) -> None:
        popup = QApplication.activePopupWidget()
        if not isinstance(popup, QMenu):
            if attempt < 40:
                QTimer.singleShot(10, lambda: act(attempt + 1))
            return
        if position < 0:
            popup.close()
            return
        for _ in range(position + 1):
            QTest.keyClick(popup, Qt.Key.Key_Down)
        QTest.keyClick(popup, Qt.Key.Key_Return)

    QTimer.singleShot(10, lambda: act())


def test_manual_radius_mode_opens_the_pick_menu_and_escapes(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    path = (PosePoint(0, 0, 0), PosePoint(1, 0, 0.4), PosePoint(2, 0, 0.8))
    canvas.set_paths(path, (), VehicleDimensions(1, 1, 1))
    items_before = len(canvas.scene().items())

    canvas.set_manual_radius_mode("dispatched")

    # Entering the mode alone must not draw anything; markers only follow a pick.
    assert len(canvas.scene().items()) == items_before
    point = canvas.mapFromScene(1, 0)
    _drive_pick_menu(1)
    with qtbot.waitSignal(canvas.radius_endpoint_selected, timeout=2000) as selected:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert selected.args == ["dispatched", 1]

    with qtbot.waitSignal(canvas.manual_radius_cancelled, timeout=1000) as cancelled:
        qtbot.keyClick(canvas, Qt.Key.Key_Escape)
    assert cancelled.args == ["dispatched"]


def test_pick_menu_offers_neighbours_so_a_nearby_sample_can_be_chosen(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    path = tuple(PosePoint(index, 0, 0) for index in range(9))
    canvas.set_paths(path, (), VehicleDimensions(1, 1, 1))
    canvas.set_manual_radius_mode("dispatched")
    point = canvas.mapFromScene(4, 0)

    # The menu lists samples 2..6; the third entry is the sample two before the click.
    _drive_pick_menu(0)
    with qtbot.waitSignal(canvas.radius_endpoint_selected, timeout=2000) as selected:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)

    assert selected.args == ["dispatched", 2]


def test_dismissing_the_pick_menu_selects_no_endpoint(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    path = tuple(PosePoint(index, 0, 0) for index in range(9))
    canvas.set_paths(path, (), VehicleDimensions(1, 1, 1))
    canvas.set_manual_radius_mode("dispatched")
    picked: list[tuple[str, int]] = []
    canvas.radius_endpoint_selected.connect(lambda name, index: picked.append((name, index)))

    _drive_pick_menu(-1)
    qtbot.mouseClick(
        canvas.viewport(),
        Qt.MouseButton.LeftButton,
        pos=canvas.mapFromScene(4, 0),
    )

    assert picked == []


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


def test_centerline_layer_marks_pose_points_and_drops_them_with_the_layer(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.set_paths(
        (PosePoint(0, 0, 0), PosePoint(1, 0, 0)),
        (PosePoint(0, 1, 0),),
        VehicleDimensions(1, 1, 1),
    )

    assert len(_path_points_items(canvas)) == 2

    canvas.set_path_layer("actual", centerline=False)

    assert len(_path_points_items(canvas)) == 1

    canvas.set_path_layer("dispatched", centerline=False)

    assert _path_points_items(canvas) == []


def test_clicking_path_point_under_a_lane_anchor_still_selects_the_point(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.load_layout(layout())
    canvas.set_snap_enabled(False)
    canvas.set_paths((PosePoint(3, 0, 0),), (), VehicleDimensions(1, 1, 1))
    point = canvas.mapFromScene(3, 0)

    with qtbot.waitSignal(canvas.path_point_selected, timeout=1000) as selected:
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)

    assert selected.args == ["dispatched", 0]
    assert canvas.selected_path_point == ("dispatched", 0)
    assert canvas.current_layout().lanes[0].anchors[1].point == Point2D(3, 0)
    assert canvas.undo_stack.count() == 0


def test_dragging_lane_anchor_off_a_path_point_does_not_select_the_point(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    canvas.resize(800, 500)
    qtbot.addWidget(canvas)
    canvas.show()
    canvas.load_layout(layout())
    canvas.set_snap_enabled(False)
    canvas.set_paths((PosePoint(3, 0, 0),), (), VehicleDimensions(1, 1, 1))
    start = canvas.mapFromScene(3, 0)
    end = canvas.mapFromScene(3, 2)

    qtbot.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(canvas.viewport(), pos=end)
    qtbot.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)

    moved = canvas.current_layout().lanes[0]
    assert moved.anchors[1].point.y == pytest.approx(2, abs=0.05)
    assert canvas.selected_path_point is None
    assert canvas.undo_stack.count() == 1


def test_switching_active_path_restyles_the_pose_point_markers(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    canvas.set_paths(
        (PosePoint(0, 0, 0),),
        (PosePoint(0, 1, 0),),
        VehicleDimensions(1, 1, 1),
    )

    assert _path_points_item(canvas, "dispatched").active
    assert not _path_points_item(canvas, "actual").active

    canvas.set_active_path("actual")

    assert not _path_points_item(canvas, "dispatched").active
    assert _path_points_item(canvas, "actual").active


def test_marker_diameters_returns_one_positive_diameter_per_point() -> None:
    points = [Point2D(index * 0.01, 0) for index in range(200)]

    diameters = marker_diameters(points, 12.5, 7.0, 1.4)

    assert len(diameters) == len(points)
    assert all(diameter > 0 for diameter in diameters)


def test_marker_diameters_keeps_the_base_size_where_points_are_sparse() -> None:
    points = [Point2D(index * 3.0, 0) for index in range(4)]

    assert marker_diameters(points, 12.5, 7.0, 1.4) == (7.0, 7.0, 7.0, 7.0)


def test_marker_diameters_shrink_with_the_screen_gap_where_points_are_crowded() -> None:
    points = [Point2D(index * 0.2, 0) for index in range(3)]

    # 0.2 m at 20 px/m is a 4 px gap, well under the 7 px base.
    expected = 4.0 * PATH_POINT_CROWDING_FACTOR
    assert marker_diameters(points, 20.0, 7.0, 1.4) == (expected, expected, expected)


def test_marker_diameters_use_the_tighter_of_the_two_neighbour_gaps() -> None:
    points = [Point2D(0, 0), Point2D(0.1, 0), Point2D(3.0, 0)]

    diameters = marker_diameters(points, 20.0, 7.0, 1.4)

    # The middle point is 2 px from its left neighbour and 58 px from its right one.
    assert diameters[1] == pytest.approx(2.0 * PATH_POINT_CROWDING_FACTOR)
    assert diameters[2] == 7.0


def test_marker_diameters_floor_coincident_points_instead_of_dropping_them() -> None:
    points = [Point2D(0, 0), Point2D(0, 0), Point2D(1, 0)]

    diameters = marker_diameters(points, 500.0, 7.0, 1.4)

    assert len(diameters) == 3
    assert diameters[0] == 1.4
    assert diameters[1] == 1.4


def test_marker_diameters_keep_degenerate_input_at_the_base_size() -> None:
    points = [Point2D(index * 0.1, 0) for index in range(4)]

    assert marker_diameters([], 20.0, 7.0, 1.4) == ()
    assert marker_diameters([Point2D(0, 0)], 20.0, 7.0, 1.4) == (7.0,)
    assert marker_diameters(points, 0.0, 7.0, 1.4) == (7.0, 7.0, 7.0, 7.0)
    assert marker_diameters(points, math.inf, 7.0, 1.4) == (7.0, 7.0, 7.0, 7.0)


def _drawn_marker_count(item: PathPointsItem, pixels_per_meter: float, width: int) -> int:
    """Paint the item onto a white strip and count the separate coloured runs."""
    image = QImage(width, 40, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(4, 20)
    painter.scale(pixels_per_meter, -pixels_per_meter)
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(-1e6, -1e6, 2e6, 2e6)
    item.paint(painter, option, None)
    painter.end()
    runs = 0
    inside = False
    for x in range(width):
        painted = QColor(image.pixelColor(x, 20)) != QColor("#ffffff")
        if painted and not inside:
            runs += 1
        inside = painted
    return runs


def test_every_pose_point_is_drawn_as_its_own_marker(qtbot: QtBot) -> None:
    points = [Point2D(index * 1.0, 0) for index in range(9)]
    item = PathPointsItem(
        points,
        QColor("#2474d8"),
        active=True,
        hollow_indices=frozenset(),
    )

    assert _drawn_marker_count(item, 30.0, 280) == len(points)


def test_crowded_pose_points_shrink_but_all_of_them_are_still_drawn(qtbot: QtBot) -> None:
    # 0.25 m apart at 30 px/m is a 7.5 px gap, so markers shrink below the 7 px base.
    points = [Point2D(index * 0.25, 0) for index in range(20)]
    item = PathPointsItem(
        points,
        QColor("#2474d8"),
        active=True,
        hollow_indices=frozenset(),
    )

    assert _drawn_marker_count(item, 30.0, 180) == len(points)


def _radius_marker_labels(canvas: RouteCanvas) -> list[str]:
    return sorted(
        item.toPlainText()
        for item in canvas.scene().items()
        if isinstance(item, QGraphicsTextItem)
    )


def test_neighbourhood_indices_covers_two_samples_on_each_side() -> None:
    assert neighbourhood_indices(10, 5) == (3, 4, 5, 6, 7)


def test_neighbourhood_indices_shortens_at_both_ends_instead_of_shifting() -> None:
    assert neighbourhood_indices(10, 0) == (0, 1, 2)
    assert neighbourhood_indices(10, 1) == (0, 1, 2, 3)
    assert neighbourhood_indices(10, 9) == (7, 8, 9)
    assert neighbourhood_indices(3, 1) == (0, 1, 2)


def test_neighbourhood_indices_rejects_out_of_range_centres_and_bad_spans() -> None:
    assert neighbourhood_indices(0, 0) == ()
    assert neighbourhood_indices(5, 5) == ()
    assert neighbourhood_indices(5, -1) == ()
    with pytest.raises(ValueError, match="span"):
        neighbourhood_indices(5, 2, -1)


def test_only_the_picked_endpoint_is_highlighted_on_the_canvas(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    path = tuple(PosePoint(index, 0, 0) for index in range(8))
    canvas.set_paths(
        path,
        (),
        VehicleDimensions(1, 1, 1),
        source_indices={"dispatched": tuple(range(10, 18))},
    )
    canvas.set_manual_radius_mode("dispatched")

    canvas.set_manual_radius_endpoints((4,))

    # Neighbours stay plain centerline points; only source index 14 is marked, as "15".
    assert _radius_marker_labels(canvas) == ["15"]


def test_both_manual_radius_endpoints_stay_marked_until_the_markers_are_cleared(
    qtbot: QtBot,
) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    path = tuple(PosePoint(index, 0, 0) for index in range(12))
    canvas.set_paths(path, (), VehicleDimensions(1, 1, 1))
    canvas.set_manual_radius_mode("dispatched")

    canvas.set_manual_radius_endpoints((2, 9))

    assert _radius_marker_labels(canvas) == ["10", "3"]

    canvas.set_manual_radius_endpoints(())

    assert _radius_marker_labels(canvas) == []


def test_radius_candidate_indices_offer_the_closest_sample_and_its_neighbours() -> None:
    assert radius_candidate_indices(20, [8]) == (6, 7, 8, 9, 10)


def test_radius_candidate_indices_keep_far_apart_hits_reachable() -> None:
    # A doubled-back path can put two hit samples far apart in path order.
    assert radius_candidate_indices(20, [8, 17]) == (6, 7, 8, 9, 10, 17)


def test_radius_candidate_indices_shorten_at_the_path_ends() -> None:
    assert radius_candidate_indices(20, [0]) == (0, 1, 2)
    assert radius_candidate_indices(20, [19]) == (17, 18, 19)
    assert radius_candidate_indices(3, [1]) == (0, 1, 2)


def test_radius_candidate_indices_drop_out_of_range_hits_and_empty_input() -> None:
    assert radius_candidate_indices(20, []) == ()
    assert radius_candidate_indices(20, [8, 99, -3]) == (6, 7, 8, 9, 10)


def test_leaving_manual_radius_mode_clears_the_endpoint_markers(qtbot: QtBot) -> None:
    canvas = RouteCanvas()
    qtbot.addWidget(canvas)
    path = tuple(PosePoint(index, 0, 0) for index in range(8))
    canvas.set_paths(path, (), VehicleDimensions(1, 1, 1))
    canvas.set_manual_radius_mode("dispatched")
    canvas.set_manual_radius_endpoints((4,))

    canvas.set_manual_radius_mode(None)

    assert _radius_marker_labels(canvas) == []
