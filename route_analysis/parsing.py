"""Strict parsers for scheduler command JSON at the external boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

from route_analysis.errors import DataContractError
from route_analysis.models import CommandPathData, PathPointData, PosePoint


def _number(value: object, field: str, index: int, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise DataContractError(f"路径点 {index} 的 {field} 不是有效数字")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DataContractError(f"路径点 {index} 的 {field} 不是有效数字") from exc
    if not math.isfinite(result):
        raise DataContractError(f"路径点 {index} 的 {field} 不是有限数字")
    return result


def _decode_command(payload: object) -> Mapping[str, object]:
    decoded = payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DataContractError("命令路径不是有效 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise DataContractError("命令路径根节点必须是对象")
    return decoded


def _point_number(
    value: object,
    field: str,
    index: int,
    *,
    optional: bool = False,
) -> tuple[float | None, str | None]:
    try:
        return _number(value, field, index, optional=optional), None
    except DataContractError as exc:
        return None, str(exc)


def parse_command_details(payload: object) -> CommandPathData:
    """Parse a command while retaining every raw source point, including invalid rows."""

    decoded = _decode_command(payload)
    raw_points = decoded.get("positionList")
    if not isinstance(raw_points, list):
        raise DataContractError("命令路径 positionList 必须是数组")

    points: list[PathPointData] = []
    for index, raw_point in enumerate(raw_points):
        if not isinstance(raw_point, Mapping):
            points.append(
                PathPointData(
                    index,
                    raw_point,
                    None,
                    None,
                    None,
                    None,
                    (f"路径点 {index} 必须是对象",),
                )
            )
            continue
        x, x_error = _point_number(raw_point.get("x"), "x", index)
        y, y_error = _point_number(raw_point.get("y"), "y", index)
        yaw, yaw_error = _point_number(raw_point.get("yaw"), "yaw", index, optional=True)
        errors = tuple(error for error in (x_error, y_error, yaw_error) if error is not None)
        points.append(
            PathPointData(
                index,
                dict(raw_point),
                x,
                y,
                yaw,
                raw_point.get("gear"),
                errors,
            )
        )
    return CommandPathData(dict(decoded), tuple(points))


def parse_command_path(payload: object) -> tuple[PosePoint, ...]:
    """Parse an ``AgvTaskCommand`` into strictly validated geometry poses."""

    details = parse_command_details(payload)
    first_error = next((error for point in details.points for error in point.errors), None)
    if first_error is not None:
        raise DataContractError(first_error)
    return details.poses
