"""Metric route canvas and undoable lane editor."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import ClassVar
from uuid import uuid4

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QUndoCommand,
    QUndoStack,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsScene, QGraphicsView
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from route_analysis.geometry import build_lane_area, vehicle_polygon
from route_analysis.models import (
    AnalysisResult,
    JoinStyle,
    Lane,
    LaneSegment,
    Point2D,
    PosePoint,
    SegmentKind,
    VehicleDimensions,
)
from route_analysis.storage import LaneLayout


@dataclass(slots=True)
class PathVisibility:
    centerline: bool = True
    vehicles: bool = True
    violations: bool = True


@dataclass(frozen=True, slots=True)
class _DragTarget:
    kind: str
    lane_id: str
    index: int
    control_number: int = 0


class _LayoutCommand(QUndoCommand):
    def __init__(
        self,
        canvas: RouteCanvas,
        before: LaneLayout,
        after: LaneLayout,
        text: str,
    ) -> None:
        super().__init__(text)
        self._canvas = canvas
        self._before = before
        self._after = after

    def undo(self) -> None:
        self._canvas._set_layout_internal(copy.deepcopy(self._before))

    def redo(self) -> None:
        self._canvas._set_layout_internal(copy.deepcopy(self._after))


class RouteCanvas(QGraphicsView):
    """A Y-up, equal-scale scene whose editable data remains in raw coordinates."""

    layout_changed = Signal()
    selection_changed = Signal(str, int, int)
    mouse_coordinate_changed = Signal(float, float)
    drawing_state_changed = Signal(bool)

    PATH_COLORS: ClassVar[dict[str, QColor]] = {
        "dispatched": QColor("#2474d8"),
        "actual": QColor("#d86b1f"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("routeCanvas")
        self.setAccessibleName("路径与车道分析画布")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setScene(QGraphicsScene(self))
        self.scene().setSceneRect(QRectF(-10000, -10000, 20000, 20000))
        self.scale(36, -36)

        self._layout = LaneLayout("0000000000000000", "0", [])
        self._map_direction = 0.0
        self._paths: dict[str, tuple[PosePoint, ...]] = {"dispatched": (), "actual": ()}
        self._dimensions: VehicleDimensions | None = None
        self._bezier_tolerance = 0.02
        self._miter_limit = 4.0
        self._results: dict[str, AnalysisResult | None] = {
            "dispatched": None,
            "actual": None,
        }
        self._visibility = {
            "dispatched": PathVisibility(),
            "actual": PathVisibility(),
        }
        self._snap_enabled = True
        self._selected_lane_id: str | None = None
        self._selected_anchor = -1
        self._selected_segment = -1
        self._drawing = False
        self._draft_points: list[Point2D] = []
        self._draft_width = 2.0
        self._draft_name = "新车道"
        self._drag_target: _DragTarget | None = None
        self._drag_before: LaneLayout | None = None

        self.undo_stack = QUndoStack(self)
        self._rebuild_scene()

    @property
    def map_direction(self) -> float:
        return self._map_direction

    @property
    def path_point_counts(self) -> dict[str, int]:
        return {name: len(points) for name, points in self._paths.items()}

    @property
    def missing_yaw_counts(self) -> dict[str, int]:
        return {
            name: sum(point.yaw is None for point in points)
            for name, points in self._paths.items()
        }

    @property
    def selected_lane_id(self) -> str | None:
        return self._selected_lane_id

    def current_layout(self) -> LaneLayout:
        return copy.deepcopy(self._layout)

    def load_layout(self, layout: LaneLayout) -> None:
        self._layout = copy.deepcopy(layout)
        self._selected_lane_id = layout.lanes[0].id if layout.lanes else None
        self._selected_anchor = 0 if layout.lanes else -1
        self._selected_segment = 0 if layout.lanes and layout.lanes[0].segments else -1
        self.undo_stack.clear()
        self.undo_stack.setClean()
        self._rebuild_scene()
        self._emit_selection()
        self.fit_content()

    def mark_saved(self) -> None:
        self.undo_stack.setClean()

    def _set_layout_internal(self, layout: LaneLayout) -> None:
        self._layout = layout
        if self._selected_lane_id and not any(
            lane.id == self._selected_lane_id for lane in layout.lanes
        ):
            self._selected_lane_id = layout.lanes[0].id if layout.lanes else None
        self._rebuild_scene()
        self.layout_changed.emit()
        self._emit_selection()

    def _mutate(self, text: str, operation: Callable[[LaneLayout], None]) -> None:
        before = copy.deepcopy(self._layout)
        after = copy.deepcopy(self._layout)
        operation(after)
        if before == after:
            return
        self.undo_stack.push(_LayoutCommand(self, before, after, text))

    @staticmethod
    def _lane(layout: LaneLayout, lane_id: str) -> Lane:
        for lane in layout.lanes:
            if lane.id == lane_id:
                return lane
        raise KeyError(f"lane not found: {lane_id}")

    def set_map_direction(self, radians: float) -> None:
        if not math.isfinite(radians):
            raise ValueError("map direction must be finite")
        self._map_direction = radians
        self._rebuild_scene()
        self.fit_content()

    def set_snap_enabled(self, enabled: bool) -> None:
        self._snap_enabled = enabled

    def set_geometry_settings(self, *, bezier_tolerance: float, miter_limit: float) -> None:
        if bezier_tolerance <= 0 or miter_limit <= 0:
            raise ValueError("geometry settings must be greater than zero")
        self._bezier_tolerance = bezier_tolerance
        self._miter_limit = miter_limit
        self._rebuild_scene()

    def to_display(self, point: Point2D) -> Point2D:
        cosine = math.cos(self._map_direction)
        sine = math.sin(self._map_direction)
        return Point2D(point.x * cosine - point.y * sine, point.x * sine + point.y * cosine)

    def to_raw(self, point: Point2D) -> Point2D:
        cosine = math.cos(self._map_direction)
        sine = math.sin(self._map_direction)
        return Point2D(point.x * cosine + point.y * sine, -point.x * sine + point.y * cosine)

    def set_paths(
        self,
        dispatched: Iterable[PosePoint],
        actual: Iterable[PosePoint],
        dimensions: VehicleDimensions,
    ) -> None:
        self._paths = {"dispatched": tuple(dispatched), "actual": tuple(actual)}
        self._dimensions = dimensions
        self._results = {"dispatched": None, "actual": None}
        self._rebuild_scene()
        self.fit_content()

    def clear_paths(self) -> None:
        self._paths = {"dispatched": (), "actual": ()}
        self._results = {"dispatched": None, "actual": None}
        self._rebuild_scene()
        self.fit_content()

    def fit_content(self) -> None:
        """Fit rendered data into the viewport while preserving the Y-up metric scale."""

        bounds = self.scene().itemsBoundingRect()
        if bounds.isEmpty() or self.viewport().width() <= 20 or self.viewport().height() <= 20:
            return
        padding = max(0.5, max(bounds.width(), bounds.height()) * 0.08)
        bounds = bounds.adjusted(-padding, -padding, padding, padding)
        scale_x = (self.viewport().width() - 20) / max(bounds.width(), 1e-9)
        scale_y = (self.viewport().height() - 20) / max(bounds.height(), 1e-9)
        metric_scale = min(500.0, max(4.0, min(scale_x, scale_y)))
        self.resetTransform()
        self.scale(metric_scale, -metric_scale)
        self.centerOn(bounds.center())

    def set_analysis_results(
        self,
        dispatched: AnalysisResult | None,
        actual: AnalysisResult | None,
    ) -> None:
        self._results = {"dispatched": dispatched, "actual": actual}
        self._rebuild_scene()

    def set_path_layer(
        self,
        path_name: str,
        *,
        centerline: bool | None = None,
        vehicles: bool | None = None,
        violations: bool | None = None,
    ) -> None:
        visibility = self._visibility[path_name]
        if centerline is not None:
            visibility.centerline = centerline
        if vehicles is not None:
            visibility.vehicles = vehicles
        if violations is not None:
            visibility.violations = violations
        self._rebuild_scene()

    def isolate_path(self, path_name: str | None) -> None:
        for name, visibility in self._visibility.items():
            visible = path_name is None or name == path_name
            visibility.centerline = visible
            visibility.vehicles = visible
            visibility.violations = visible
        self._rebuild_scene()

    def select_lane(self, lane_id: str | None) -> None:
        self._selected_lane_id = lane_id
        self._selected_anchor = 0 if lane_id else -1
        self._selected_segment = 0 if lane_id else -1
        self._rebuild_scene()
        self._emit_selection()

    def select_anchor(self, index: int) -> None:
        self._selected_anchor = index
        self._rebuild_scene()
        self._emit_selection()

    def select_segment(self, index: int) -> None:
        self._selected_segment = index
        self._rebuild_scene()
        self._emit_selection()

    def _emit_selection(self) -> None:
        self.selection_changed.emit(
            self._selected_lane_id or "", self._selected_anchor, self._selected_segment
        )

    def add_lane(
        self,
        points: list[Point2D],
        *,
        width: float,
        name: str,
        closed: bool = False,
    ) -> str:
        lane_id = uuid4().hex

        def operation(layout: LaneLayout) -> None:
            layout.lanes.append(Lane.create(lane_id, name, width, points, closed=closed))

        self._mutate("添加车道", operation)
        self._selected_lane_id = lane_id
        self._selected_anchor = 0
        self._selected_segment = 0
        self._rebuild_scene()
        self._emit_selection()
        return lane_id

    def delete_lane(self, lane_id: str) -> None:
        self._mutate(
            "删除车道",
            lambda layout: setattr(
                layout, "lanes", [lane for lane in layout.lanes if lane.id != lane_id]
            ),
        )

    def set_lane_name(self, lane_id: str, name: str) -> None:
        self._mutate(
            "重命名车道",
            lambda layout: setattr(self._lane(layout, lane_id), "name", name),
        )

    def set_lane_enabled(self, lane_id: str, enabled: bool) -> None:
        self._mutate(
            "启用或禁用车道",
            lambda layout: setattr(self._lane(layout, lane_id), "enabled", enabled),
        )

    def set_lane_width(self, lane_id: str, width: float) -> None:
        if width <= 0:
            raise ValueError("lane width must be greater than zero")
        self._mutate(
            "修改车道宽度",
            lambda layout: setattr(self._lane(layout, lane_id), "width", width),
        )

    def set_lane_default_join(self, lane_id: str, join: JoinStyle) -> None:
        self._mutate(
            "修改车道连接样式",
            lambda layout: setattr(self._lane(layout, lane_id), "default_join", join),
        )

    def set_lane_closed(self, lane_id: str, closed: bool) -> None:
        def operation(layout: LaneLayout) -> None:
            lane = self._lane(layout, lane_id)
            if lane.closed == closed:
                return
            lane.closed = closed
            if closed:
                lane.segments.append(LaneSegment())
            else:
                lane.segments.pop()

        self._mutate("切换开放或闭合车道", operation)

    def set_anchor_position(self, lane_id: str, index: int, point: Point2D) -> None:
        self._mutate(
            "移动车道锚点",
            lambda layout: setattr(self._lane(layout, lane_id).anchors[index], "point", point),
        )

    def set_anchor_join(self, lane_id: str, index: int, join: JoinStyle | None) -> None:
        self._mutate(
            "修改锚点连接样式",
            lambda layout: setattr(
                self._lane(layout, lane_id).anchors[index], "join_override", join
            ),
        )

    def set_segment_kind(self, lane_id: str, index: int, kind: SegmentKind) -> None:
        def operation(layout: LaneLayout) -> None:
            lane = self._lane(layout, lane_id)
            segment = lane.segments[index]
            if segment.kind is kind:
                return
            segment.kind = kind
            if kind is SegmentKind.LINE:
                segment.control1 = None
                segment.control2 = None
                return
            start = lane.anchors[index].point
            end = lane.anchors[(index + 1) % len(lane.anchors)].point
            segment.control1 = Point2D(
                start.x + (end.x - start.x) / 3,
                start.y + (end.y - start.y) / 3,
            )
            segment.control2 = Point2D(
                start.x + 2 * (end.x - start.x) / 3,
                start.y + 2 * (end.y - start.y) / 3,
            )

        self._mutate("修改线段类型", operation)

    def set_control_point(
        self,
        lane_id: str,
        segment_index: int,
        control_number: int,
        point: Point2D,
    ) -> None:
        if control_number not in {1, 2}:
            raise ValueError("control number must be 1 or 2")

        def operation(layout: LaneLayout) -> None:
            segment = self._lane(layout, lane_id).segments[segment_index]
            if segment.kind is not SegmentKind.CUBIC:
                raise ValueError("line segments do not have control points")
            setattr(segment, f"control{control_number}", point)

        self._mutate("移动贝塞尔控制点", operation)

    def start_lane_drawing(self, *, width: float, name: str = "新车道") -> None:
        if width <= 0:
            raise ValueError("lane width must be greater than zero")
        self._drawing = True
        self._draft_points.clear()
        self._draft_width = width
        self._draft_name = name
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.drawing_state_changed.emit(True)
        self._rebuild_scene()

    def cancel_lane_drawing(self) -> None:
        self._drawing = False
        self._draft_points.clear()
        self.unsetCursor()
        self.drawing_state_changed.emit(False)
        self._rebuild_scene()

    def finish_lane_drawing(self) -> str | None:
        if len(self._draft_points) < 2:
            return None
        points = self._draft_points.copy()
        self.cancel_lane_drawing()
        return self.add_lane(points, width=self._draft_width, name=self._draft_name)

    def _snap(self, point: Point2D) -> Point2D:
        if not self._snap_enabled:
            return point
        threshold = 10 / max(abs(self.transform().m11()), 1)
        candidates: list[PosePoint] = []
        for name, points in self._paths.items():
            visibility = self._visibility[name]
            if visibility.centerline or visibility.vehicles:
                candidates.extend(points)
        if not candidates:
            return point
        nearest = min(candidates, key=lambda pose: math.hypot(pose.x - point.x, pose.y - point.y))
        if math.hypot(nearest.x - point.x, nearest.y - point.y) <= threshold:
            return Point2D(nearest.x, nearest.y)
        return point

    def _raw_from_event(self, event: QMouseEvent) -> Point2D:
        scene_position = self.mapToScene(event.position().toPoint())
        return self._snap(self.to_raw(Point2D(scene_position.x(), scene_position.y())))

    def _hit_test(self, raw: Point2D) -> _DragTarget | None:
        threshold = 10 / max(abs(self.transform().m11()), 1)
        lane_order = sorted(
            self._layout.lanes,
            key=lambda lane: lane.id != self._selected_lane_id,
        )
        for lane in lane_order:
            for index, anchor in enumerate(lane.anchors):
                if math.hypot(anchor.point.x - raw.x, anchor.point.y - raw.y) <= threshold:
                    self._selected_lane_id = lane.id
                    self._selected_anchor = index
                    return _DragTarget("anchor", lane.id, index)
            for index, segment in enumerate(lane.segments):
                if segment.kind is not SegmentKind.CUBIC:
                    continue
                for number, control in ((1, segment.control1), (2, segment.control2)):
                    if control and math.hypot(control.x - raw.x, control.y - raw.y) <= threshold:
                        self._selected_lane_id = lane.id
                        self._selected_segment = index
                        return _DragTarget("control", lane.id, index, number)
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            raw = self._raw_from_event(event)
            if self._drawing:
                if not self._draft_points or self._draft_points[-1] != raw:
                    self._draft_points.append(raw)
                self._rebuild_scene()
                return
            target = self._hit_test(raw)
            if target is not None:
                self._drag_target = target
                self._drag_before = copy.deepcopy(self._layout)
                self._emit_selection()
                self._rebuild_scene()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._drawing and event.button() == Qt.MouseButton.LeftButton:
            raw = self._raw_from_event(event)
            if not self._draft_points or self._draft_points[-1] != raw:
                self._draft_points.append(raw)
            self.finish_lane_drawing()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        scene_position = self.mapToScene(event.position().toPoint())
        raw = self.to_raw(Point2D(scene_position.x(), scene_position.y()))
        self.mouse_coordinate_changed.emit(raw.x, raw.y)
        if self._drag_target is not None and event.buttons() & Qt.MouseButton.LeftButton:
            snapped = self._snap(raw)
            lane = self._lane(self._layout, self._drag_target.lane_id)
            if self._drag_target.kind == "anchor":
                lane.anchors[self._drag_target.index].point = snapped
            else:
                segment = lane.segments[self._drag_target.index]
                setattr(segment, f"control{self._drag_target.control_number}", snapped)
            self._rebuild_scene()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_target is not None and event.button() == Qt.MouseButton.LeftButton:
            before = self._drag_before
            after = copy.deepcopy(self._layout)
            self._drag_target = None
            self._drag_before = None
            if before is not None and before != after:
                self.undo_stack.push(_LayoutCommand(self, before, after, "拖动车道控制点"))
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._drawing and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.finish_lane_drawing()
            return
        if self._drawing and event.key() == Qt.Key.Key_Escape:
            self.cancel_lane_drawing()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        current = abs(self.transform().m11())
        if 4 <= current * factor <= 500:
            self.scale(factor, factor)
        event.accept()

    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        painter.fillRect(rect, QColor("#f7f9fc"))
        pixels_per_meter = abs(self.transform().m11())
        step = 1.0
        while pixels_per_meter * step < 24:
            step *= 2
        while pixels_per_meter * step > 100:
            step /= 2
        left = math.floor(rect.left() / step) * step
        right = math.ceil(rect.right() / step) * step
        top = math.floor(rect.top() / step) * step
        bottom = math.ceil(rect.bottom() / step) * step
        grid_pen = QPen(QColor("#dfe5ee"))
        grid_pen.setCosmetic(True)
        painter.setPen(grid_pen)
        x = left
        while x <= right:
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            x += step
        y = top
        while y <= bottom:
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            y += step
        axis_pen = QPen(QColor("#8290a3"), 1.5)
        axis_pen.setCosmetic(True)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(left, 0), QPointF(right, 0))
        painter.drawLine(QPointF(0, top), QPointF(0, bottom))

    @staticmethod
    def _cosmetic_pen(
        color: QColor,
        width: float,
        style: Qt.PenStyle = Qt.PenStyle.SolidLine,
    ) -> QPen:
        pen = QPen(color, width, style)
        pen.setCosmetic(True)
        return pen

    def _display_polygon(self, coordinates: Iterable[tuple[float, float]]) -> QPolygonF:
        polygon = QPolygonF()
        for x, y in coordinates:
            point = self.to_display(Point2D(x, y))
            polygon.append(QPointF(point.x, point.y))
        return polygon

    def _geometry_path(self, geometry: BaseGeometry) -> QPainterPath:
        painter_path = QPainterPath()
        painter_path.setFillRule(Qt.FillRule.OddEvenFill)
        polygons: Iterable[Polygon]
        if isinstance(geometry, Polygon):
            polygons = [geometry]
        elif isinstance(geometry, MultiPolygon):
            polygons = geometry.geoms
        else:
            polygons = [
                item for item in getattr(geometry, "geoms", ()) if isinstance(item, Polygon)
            ]
        for polygon in polygons:
            painter_path.addPolygon(self._display_polygon(polygon.exterior.coords))
            for interior in polygon.interiors:
                painter_path.addPolygon(self._display_polygon(interior.coords))
        return painter_path

    def _add_lane_graphics(self, lane: Lane) -> None:
        area = build_lane_area(
            lane,
            tolerance=self._bezier_tolerance,
            miter_limit=self._miter_limit,
        )
        area_item = QGraphicsPathItem(self._geometry_path(area))
        area_color = QColor("#4f8dd8" if lane.enabled else "#aab3bf")
        area_color.setAlpha(55 if lane.enabled else 30)
        area_item.setBrush(QBrush(area_color))
        area_item.setPen(self._cosmetic_pen(QColor("#3972b6"), 1))
        area_item.setZValue(-10)
        self.scene().addItem(area_item)

        center_path = QPainterPath()
        first = self.to_display(lane.anchors[0].point)
        center_path.moveTo(first.x, first.y)
        for index, segment in enumerate(lane.segments):
            end = self.to_display(lane.anchors[(index + 1) % len(lane.anchors)].point)
            if segment.kind is SegmentKind.CUBIC and segment.control1 and segment.control2:
                control1 = self.to_display(segment.control1)
                control2 = self.to_display(segment.control2)
                center_path.cubicTo(
                    control1.x,
                    control1.y,
                    control2.x,
                    control2.y,
                    end.x,
                    end.y,
                )
            else:
                center_path.lineTo(end.x, end.y)
        center_item = self.scene().addPath(
            center_path,
            self._cosmetic_pen(QColor("#225a9d"), 1.5, Qt.PenStyle.DashLine),
        )
        center_item.setZValue(-5)

        if lane.id != self._selected_lane_id:
            return
        handle_pen = self._cosmetic_pen(QColor("#14395f"), 1)
        for index, anchor in enumerate(lane.anchors):
            display = self.to_display(anchor.point)
            radius = 0.11 if index == self._selected_anchor else 0.08
            anchor_item = self.scene().addEllipse(
                display.x - radius,
                display.y - radius,
                radius * 2,
                radius * 2,
                handle_pen,
                QBrush(QColor("#ffffff")),
            )
            anchor_item.setZValue(20)
        for index, segment in enumerate(lane.segments):
            if (
                segment.kind is not SegmentKind.CUBIC
                or not segment.control1
                or not segment.control2
            ):
                continue
            start = self.to_display(lane.anchors[index].point)
            end = self.to_display(lane.anchors[(index + 1) % len(lane.anchors)].point)
            control1 = self.to_display(segment.control1)
            control2 = self.to_display(segment.control2)
            guide = QPainterPath(QPointF(start.x, start.y))
            guide.lineTo(control1.x, control1.y)
            guide.moveTo(end.x, end.y)
            guide.lineTo(control2.x, control2.y)
            self.scene().addPath(
                guide,
                self._cosmetic_pen(QColor("#6e7f93"), 1, Qt.PenStyle.DotLine),
            )
            for control in (control1, control2):
                control_item = self.scene().addRect(
                    control.x - 0.07,
                    control.y - 0.07,
                    0.14,
                    0.14,
                    handle_pen,
                    QBrush(QColor("#ffd166")),
                )
                control_item.setZValue(21)

    def _add_route_graphics(self, name: str) -> None:
        points = self._paths[name]
        if not points:
            return
        visibility = self._visibility[name]
        color = self.PATH_COLORS[name]
        if visibility.centerline:
            path = QPainterPath()
            first = self.to_display(Point2D(points[0].x, points[0].y))
            path.moveTo(first.x, first.y)
            for pose in points[1:]:
                display = self.to_display(Point2D(pose.x, pose.y))
                path.lineTo(display.x, display.y)
            route_item = self.scene().addPath(path, self._cosmetic_pen(color, 2.2))
            route_item.setZValue(5)

        if visibility.vehicles and self._dimensions is not None:
            fill = QColor(color)
            fill.setAlpha(24)
            for pose in points:
                display = self.to_display(Point2D(pose.x, pose.y))
                if pose.yaw is None:
                    self.scene().addEllipse(
                        display.x - 0.09,
                        display.y - 0.09,
                        0.18,
                        0.18,
                        self._cosmetic_pen(QColor("#6b7280"), 2),
                        QBrush(QColor("#ffffff")),
                    ).setZValue(8)
                    continue
                polygon = vehicle_polygon(pose, self._dimensions)
                vehicle_item = self.scene().addPolygon(
                    self._display_polygon(polygon.exterior.coords),
                    self._cosmetic_pen(color, 1),
                    QBrush(fill),
                )
                vehicle_item.setZValue(6)

        result = self._results[name]
        if visibility.violations and result is not None:
            for assessment in result.assessments:
                if not assessment.outside:
                    continue
                display = self.to_display(Point2D(assessment.pose.x, assessment.pose.y))
                marker_item = self.scene().addEllipse(
                    display.x - 0.1,
                    display.y - 0.1,
                    0.2,
                    0.2,
                    self._cosmetic_pen(QColor("#a10f2b"), 1.5),
                    QBrush(QColor("#ef476f")),
                )
                marker_item.setZValue(30)

    def _add_draft_graphics(self) -> None:
        if not self._draft_points:
            return
        path = QPainterPath()
        first = self.to_display(self._draft_points[0])
        path.moveTo(first.x, first.y)
        for raw in self._draft_points[1:]:
            display = self.to_display(raw)
            path.lineTo(display.x, display.y)
        self.scene().addPath(
            path,
            self._cosmetic_pen(QColor("#00a884"), 2, Qt.PenStyle.DashLine),
        ).setZValue(40)

    def _rebuild_scene(self) -> None:
        self.scene().clear()
        for lane in self._layout.lanes:
            self._add_lane_graphics(lane)
        self._add_route_graphics("dispatched")
        self._add_route_graphics("actual")
        if self._drawing:
            self._add_draft_graphics()
