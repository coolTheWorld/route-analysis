"""Versioned local JSON storage for settings, vehicle profiles, and lanes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from route_analysis.api_client import ConnectionSettings
from route_analysis.errors import DataContractError, ImportMismatchError, StorageError
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


def _atomic_json_write(path: Path, payload: Mapping[str, object], *, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if backup and path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temporary, path)
    except OSError as exc:
        raise StorageError(f"无法写入本地文件：{path.name}") from exc


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取本地文件：{path.name}") from exc
    if not isinstance(payload, Mapping):
        raise StorageError(f"本地文件根节点必须是对象：{path.name}")
    return payload


def _vehicle_to_dict(vehicle: VehicleDimensions) -> dict[str, float]:
    return {
        "width": vehicle.width,
        "center_front": vehicle.center_front,
        "center_rear": vehicle.center_rear,
    }


def _vehicle_from_dict(payload: object) -> VehicleDimensions | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise StorageError("车辆尺寸配置必须是对象")
    try:
        return VehicleDimensions(
            _float_value(payload["width"]),
            _float_value(payload["center_front"]),
            _float_value(payload["center_rear"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError("车辆尺寸配置无效") from exc


def _float_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("not a number")
    return float(value)


@dataclass(slots=True)
class AppConfig:
    api_root: str = ""
    tenant: str = "suntae"
    username: str = ""
    password: str = ""
    timeout_seconds: float = 20.0
    verify_tls: bool = True
    default_vehicle: VehicleDimensions | None = None
    default_lane_width: float | None = None
    map_direction: float = 0.0
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    snap_to_path: bool = True
    lane_generation_mode: str = "sharp"
    lane_connection: str = "path"
    log_level: str = "INFO"

    @property
    def first_run_complete(self) -> bool:
        return self.default_vehicle is not None and (
            self.default_lane_width is not None
            and math.isfinite(self.default_lane_width)
            and self.default_lane_width > 0
        )

    def connection(self) -> ConnectionSettings:
        return ConnectionSettings(
            self.api_root,
            self.tenant,
            self.username,
            self.password,
            self.timeout_seconds,
            self.verify_tls,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "connection": {
                "api_root": self.api_root,
                "tenant": self.tenant,
                "username": self.username,
                "password": self.password,
                "timeout_seconds": self.timeout_seconds,
                "verify_tls": self.verify_tls,
            },
            "vehicle": _vehicle_to_dict(self.default_vehicle) if self.default_vehicle else None,
            "default_lane_width": self.default_lane_width,
            "map_direction": self.map_direction,
            "analysis": {
                "position_step": self.analysis.position_step,
                "yaw_step": self.analysis.yaw_step,
                "clearance_threshold": self.analysis.clearance_threshold,
                "bezier_tolerance": self.analysis.bezier_tolerance,
                "miter_limit": self.analysis.miter_limit,
                "lane_generation_deviation": self.analysis.lane_generation_deviation,
            },
            "snap_to_path": self.snap_to_path,
            "lane_generation_mode": self.lane_generation_mode,
            "lane_connection": self.lane_connection,
            "log_level": self.log_level,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AppConfig:
        connection = payload.get("connection", {})
        analysis = payload.get("analysis", {})
        if not isinstance(connection, Mapping) or not isinstance(analysis, Mapping):
            raise StorageError("配置文件 connection 或 analysis 无效")
        try:
            default_width_raw = payload.get("default_lane_width")
            default_width = None if default_width_raw is None else _float_value(default_width_raw)
            map_direction = _float_value(payload.get("map_direction", 0.0))
            if not math.isfinite(map_direction):
                raise ValueError
            generation_mode = str(payload.get("lane_generation_mode", "sharp"))
            lane_connection = str(payload.get("lane_connection", "path"))
            log_level = str(payload.get("log_level", "INFO")).upper()
            if generation_mode not in {"sharp", "round", "bezier"}:
                raise ValueError
            if lane_connection not in {"path", "straight"}:
                raise ValueError
            if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
                raise ValueError
            return cls(
                api_root=str(connection.get("api_root", "")),
                tenant=str(connection.get("tenant", "suntae")),
                username=str(connection.get("username", "")),
                password=str(connection.get("password", "")),
                timeout_seconds=_float_value(connection.get("timeout_seconds", 20.0)),
                verify_tls=bool(connection.get("verify_tls", True)),
                default_vehicle=_vehicle_from_dict(payload.get("vehicle")),
                default_lane_width=default_width,
                map_direction=map_direction,
                analysis=AnalysisSettings(
                    position_step=_float_value(analysis.get("position_step", 0.05)),
                    yaw_step=_float_value(analysis.get("yaw_step", 0.02)),
                    clearance_threshold=_float_value(
                        analysis.get("clearance_threshold", 0.05)
                    ),
                    bezier_tolerance=_float_value(analysis.get("bezier_tolerance", 0.02)),
                    miter_limit=_float_value(analysis.get("miter_limit", 4.0)),
                    lane_generation_deviation=_float_value(
                        analysis.get("lane_generation_deviation", 0.05)
                    ),
                ),
                snap_to_path=bool(payload.get("snap_to_path", True)),
                lane_generation_mode=generation_mode,
                lane_connection=lane_connection,
                log_level=log_level,
            )
        except (TypeError, ValueError) as exc:
            raise StorageError("配置文件包含无效数值") from exc


class ConfigRepository:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "config.json"

    def load(self) -> AppConfig:
        return AppConfig() if not self.path.exists() else AppConfig.from_dict(_read_json(self.path))

    def save(self, config: AppConfig) -> None:
        if config.default_lane_width is not None and (
            not math.isfinite(config.default_lane_width) or config.default_lane_width <= 0
        ):
            raise StorageError("默认车道宽度必须大于零")
        if config.lane_generation_mode not in {"sharp", "round", "bezier"}:
            raise StorageError("自动车道弯道模式无效")
        if config.lane_connection not in {"path", "straight"}:
            raise StorageError("两点连接方式无效")
        if config.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise StorageError("日志级别无效")
        _atomic_json_write(self.path, config.to_dict(), backup=True)


@dataclass(frozen=True, slots=True)
class VehicleProfiles:
    values: Mapping[str, VehicleDimensions]

    def resolve(self, vin: str, default: VehicleDimensions) -> VehicleDimensions:
        return self.values.get(vin, default)


class VehicleProfileRepository:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "vehicle-profiles.json"

    def load(self) -> VehicleProfiles:
        if not self.path.exists():
            return VehicleProfiles({})
        payload = _read_json(self.path)
        raw_profiles = payload.get("profiles", {})
        if not isinstance(raw_profiles, Mapping):
            raise StorageError("VIN 车辆参数 profiles 必须是对象")
        profiles: dict[str, VehicleDimensions] = {}
        for vin, raw_vehicle in raw_profiles.items():
            vehicle = _vehicle_from_dict(raw_vehicle)
            if vehicle is None:
                raise StorageError(f"VIN {vin} 的车辆参数为空")
            profiles[str(vin)] = vehicle
        return VehicleProfiles(profiles)

    def save(self, profiles: Mapping[str, VehicleDimensions]) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "profiles": {vin: _vehicle_to_dict(vehicle) for vin, vehicle in profiles.items()},
        }
        _atomic_json_write(self.path, payload, backup=True)


def server_id_for(api_root: str) -> str:
    parsed = urlsplit(api_root.strip().rstrip("/"))
    netloc = (parsed.hostname or "").lower()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    normalized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _point_dict(point: Point2D) -> dict[str, float]:
    return {"x": point.x, "y": point.y}


def _point_from(value: object) -> Point2D:
    if not isinstance(value, Mapping):
        raise DataContractError("车道坐标必须是对象")
    try:
        return Point2D(float(value["x"]), float(value["y"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataContractError("车道坐标无效") from exc


def _lane_to_dict(lane: Lane) -> dict[str, object]:
    return {
        "id": lane.id,
        "name": lane.name,
        "width": lane.width,
        "enabled": lane.enabled,
        "closed": lane.closed,
        "defaultJoin": lane.default_join.value,
        "anchors": [
            {
                **_point_dict(anchor.point),
                "joinOverride": anchor.join_override.value if anchor.join_override else None,
            }
            for anchor in lane.anchors
        ],
        "segments": [
            {
                "kind": segment.kind.value,
                "control1": _point_dict(segment.control1) if segment.control1 else None,
                "control2": _point_dict(segment.control2) if segment.control2 else None,
                "arcCenter": _point_dict(segment.arc_center) if segment.arc_center else None,
                "clockwise": segment.clockwise,
            }
            for segment in lane.segments
        ],
    }


def _lane_from(value: object) -> Lane:
    if not isinstance(value, Mapping):
        raise DataContractError("车道必须是对象")
    raw_anchors = value.get("anchors")
    raw_segments = value.get("segments")
    if not isinstance(raw_anchors, list) or not isinstance(raw_segments, list):
        raise DataContractError("车道 anchors 和 segments 必须是数组")
    try:
        anchors = []
        for raw_anchor in raw_anchors:
            if not isinstance(raw_anchor, Mapping):
                raise DataContractError("车道锚点必须是对象")
            override = raw_anchor.get("joinOverride")
            anchors.append(
                LaneAnchor(
                    _point_from(raw_anchor),
                    JoinStyle(str(override)) if override is not None else None,
                )
            )
        segments = []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, Mapping):
                raise DataContractError("车道线段必须是对象")
            segments.append(
                LaneSegment(
                    SegmentKind(str(raw_segment.get("kind", "line"))),
                    _point_from(raw_segment["control1"])
                    if raw_segment.get("control1") is not None
                    else None,
                    _point_from(raw_segment["control2"])
                    if raw_segment.get("control2") is not None
                    else None,
                    _point_from(raw_segment["arcCenter"])
                    if raw_segment.get("arcCenter") is not None
                    else None,
                    bool(raw_segment["clockwise"])
                    if raw_segment.get("clockwise") is not None
                    else None,
                )
            )
        return Lane(
            id=str(value["id"]),
            name=str(value.get("name", value["id"])),
            width=float(value["width"]),
            anchors=anchors,
            segments=segments,
            enabled=bool(value.get("enabled", True)),
            closed=bool(value.get("closed", False)),
            default_join=JoinStyle(str(value.get("defaultJoin", "miter"))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DataContractError):
            raise
        raise DataContractError("车道记录字段无效") from exc


@dataclass(slots=True)
class LaneLayout:
    server_id: str
    map_id: str
    lanes: list[Lane]

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "serverId": self.server_id,
            "mapId": self.map_id,
            "units": {"distance": "m", "angle": "rad"},
            "lanes": [_lane_to_dict(lane) for lane in self.lanes],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LaneLayout:
        if payload.get("schemaVersion") not in (1, 2):
            raise DataContractError("不支持的车道文件版本")
        units = payload.get("units")
        valid_units = (
            isinstance(units, Mapping)
            and units.get("distance") == "m"
            and units.get("angle") == "rad"
        )
        if not valid_units:
            raise DataContractError("车道文件单位必须为米和弧度")
        raw_lanes = payload.get("lanes")
        if not isinstance(raw_lanes, list):
            raise DataContractError("车道文件 lanes 必须是数组")
        server_id = str(payload.get("serverId", "")).strip()
        map_id = str(payload.get("mapId", "")).strip()
        if not server_id or not map_id:
            raise DataContractError("车道文件缺少 serverId 或 mapId")
        return cls(server_id, map_id, [_lane_from(raw) for raw in raw_lanes])


@dataclass(frozen=True, slots=True)
class ImportPreview:
    layout: LaneLayout
    mismatches: tuple[str, ...]


class LaneRepository:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "lanes"

    def _path(self, server_id: str, map_id: str) -> Path:
        if not server_id or any(char not in "0123456789abcdef" for char in server_id.lower()):
            raise StorageError("server-id 无效")
        safe_map_id = str(map_id)
        if not safe_map_id or not safe_map_id.isdigit():
            raise StorageError("mapId 必须是数字")
        return self.root / server_id.lower() / f"{safe_map_id}.json"

    def load(self, server_id: str, map_id: str) -> LaneLayout:
        path = self._path(server_id, map_id)
        if not path.exists():
            return LaneLayout(server_id.lower(), str(map_id), [])
        try:
            return LaneLayout.from_dict(_read_json(path))
        except DataContractError as exc:
            raise StorageError(f"车道文件内容无效：{path.name}") from exc

    def save(self, layout: LaneLayout) -> Path:
        path = self._path(layout.server_id, layout.map_id)
        _atomic_json_write(path, layout.to_dict(), backup=True)
        return path

    def export(self, layout: LaneLayout, destination: Path) -> None:
        _atomic_json_write(destination, layout.to_dict())

    def preview_import(
        self,
        source: Path,
        *,
        expected_server_id: str,
        expected_map_id: str,
    ) -> ImportPreview:
        try:
            layout = LaneLayout.from_dict(_read_json(source))
        except DataContractError as exc:
            raise StorageError("导入文件内容无效") from exc
        mismatches: list[str] = []
        if layout.server_id != expected_server_id:
            mismatches.append("server_id")
        if layout.map_id != str(expected_map_id):
            mismatches.append("map_id")
        return ImportPreview(layout, tuple(mismatches))

    def replace_from_import(
        self,
        source: Path,
        target_server_id: str,
        target_map_id: str,
        *,
        allow_mismatch: bool,
    ) -> LaneLayout:
        preview = self.preview_import(
            source,
            expected_server_id=target_server_id,
            expected_map_id=target_map_id,
        )
        if preview.mismatches and not allow_mismatch:
            raise ImportMismatchError("导入文件的服务器或 mapId 与当前上下文不一致")
        replacement = LaneLayout(target_server_id, str(target_map_id), preview.layout.lanes)
        self.save(replacement)
        return replacement
