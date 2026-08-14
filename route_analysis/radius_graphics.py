"""Qt scene rendering for one selected whole-turn radius measurement."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
)

from route_analysis.geometry import vehicle_polygon
from route_analysis.models import Point2D, PosePoint, VehicleDimensions
from route_analysis.turn_radius import CornerRadiusKind, TurnRadiusSection

DisplayTransform = Callable[[Point2D], Point2D]
PolygonTransform = Callable[[Iterable[tuple[float, float]]], QPolygonF]
PenFactory = Callable[[QColor, float, Qt.PenStyle], QPen]


def add_whole_turn_graphics(
    scene: QGraphicsScene,
    path: Sequence[PosePoint],
    measurement: TurnRadiusSection,
    dimensions: VehicleDimensions,
    path_color: QColor,
    *,
    to_display: DisplayTransform,
    display_polygon: PolygonTransform,
    cosmetic_pen: PenFactory,
) -> None:
    if (
        not measurement.valid
        or measurement.rotation_center is None
        or measurement.front_axle_radius is None
    ):
        return
    start_pose = path[measurement.start_index]
    end_pose = path[measurement.end_index]
    center_raw = measurement.rotation_center

    highlight = QPainterPath()
    for index in range(measurement.start_index, measurement.end_index + 1):
        pose = path[index]
        point = to_display(Point2D(pose.x, pose.y))
        if index == measurement.start_index:
            highlight.moveTo(point.x, point.y)
        else:
            highlight.lineTo(point.x, point.y)
    highlight_color = QColor("#f4b400")
    highlight_color.setAlpha(185)
    scene.addPath(
        highlight,
        cosmetic_pen(highlight_color, 4, Qt.PenStyle.SolidLine),
    ).setZValue(55)

    fill = QColor(path_color)
    fill.setAlpha(52)
    for pose in (start_pose, end_pose):
        polygon = vehicle_polygon(pose, dimensions)
        scene.addPolygon(
            display_polygon(polygon.exterior.coords),
            cosmetic_pen(path_color, 2, Qt.PenStyle.SolidLine),
            QBrush(fill),
        ).setZValue(60)

    center = to_display(center_raw)
    cross_size = 0.16
    center_pen = cosmetic_pen(QColor("#111827"), 2, Qt.PenStyle.SolidLine)
    scene.addLine(
        center.x - cross_size,
        center.y,
        center.x + cross_size,
        center.y,
        center_pen,
    ).setZValue(70)
    scene.addLine(
        center.x,
        center.y - cross_size,
        center.x,
        center.y + cross_size,
        center_pen,
    ).setZValue(70)

    labels = {
        CornerRadiusKind.FRONT_OUTER: "前外角",
        CornerRadiusKind.REAR_OUTER: "后外角",
        CornerRadiusKind.FRONT_INNER: "前内角",
        CornerRadiusKind.REAR_INNER: "后内角",
    }
    colors = {
        CornerRadiusKind.FRONT_OUTER: QColor("#b4233f"),
        CornerRadiusKind.REAR_OUTER: QColor("#d86b1f"),
        CornerRadiusKind.FRONT_INNER: QColor("#2474d8"),
        CornerRadiusKind.REAR_INNER: QColor("#16794b"),
    }
    entries: list[tuple[str, Point2D, float, QColor]] = [
        (
            "前轴中心",
            Point2D(start_pose.x, start_pose.y),
            measurement.front_axle_radius,
            QColor("#7a3db8"),
        )
    ]
    entries.extend(
        (
            labels[kind],
            measurement.corners[kind],
            measurement.radii[kind],
            colors[kind],
        )
        for kind in CornerRadiusKind
    )
    arc_steps = max(16, min(720, math.ceil(abs(measurement.cumulative_yaw) * 48)))
    for _label, start_point, radius, radius_color in entries:
        display_start = to_display(start_point)
        scene.addLine(
            center.x,
            center.y,
            display_start.x,
            display_start.y,
            cosmetic_pen(radius_color, 1.5, Qt.PenStyle.SolidLine),
        ).setZValue(65)
        start_angle = math.atan2(
            start_point.y - center_raw.y,
            start_point.x - center_raw.x,
        )
        arc = QPainterPath()
        for sample_index in range(arc_steps + 1):
            angle = start_angle + measurement.cumulative_yaw * sample_index / arc_steps
            raw = Point2D(
                center_raw.x + radius * math.cos(angle),
                center_raw.y + radius * math.sin(angle),
            )
            display = to_display(raw)
            if sample_index == 0:
                arc.moveTo(QPointF(display.x, display.y))
            else:
                arc.lineTo(QPointF(display.x, display.y))
        scene.addPath(
            arc,
            cosmetic_pen(radius_color, 2, Qt.PenStyle.SolidLine),
        ).setZValue(66)

    maximum_radius = max(radius for _label, _point, radius, _color in entries)
    legend_anchor = QPointF(
        center.x + maximum_radius + 0.25,
        center.y + maximum_radius,
    )
    background = scene.addRect(QRectF(0, 0, 174, len(entries) * 20 + 8))
    background.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
    background.setPos(legend_anchor)
    background.setZValue(79)
    background.setPen(cosmetic_pen(QColor("#9ca3af"), 1, Qt.PenStyle.SolidLine))
    background.setBrush(QBrush(QColor(255, 255, 255, 224)))
    for row, (label, _point, radius, radius_color) in enumerate(entries):
        text_item = scene.addText(f"{label} {radius:.3f} m")
        text_item.setDefaultTextColor(radius_color)
        text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        text_item.setPos(legend_anchor)
        text_item.setTransform(QTransform.fromTranslate(4, row * 20))
        text_item.setZValue(80)
