"""Strict parsers for scheduler command JSON at the external boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

from route_analysis.errors import DataContractError
from route_analysis.models import PosePoint


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


def parse_command_path(payload: object) -> tuple[PosePoint, ...]:
    """Parse an ``AgvTaskCommand`` or its JSON string into validated poses."""

    decoded = payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DataContractError("命令路径不是有效 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise DataContractError("命令路径根节点必须是对象")
    raw_points = decoded.get("positionList")
    if not isinstance(raw_points, list):
        raise DataContractError("命令路径 positionList 必须是数组")

    points: list[PosePoint] = []
    for index, raw_point in enumerate(raw_points):
        if not isinstance(raw_point, Mapping):
            raise DataContractError(f"路径点 {index} 必须是对象")
        x = _number(raw_point.get("x"), "x", index)
        y = _number(raw_point.get("y"), "y", index)
        yaw = _number(raw_point.get("yaw"), "yaw", index, optional=True)
        assert x is not None and y is not None
        points.append(PosePoint(x, y, yaw))
    return tuple(points)
