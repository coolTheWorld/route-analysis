"""Named turn-radius measurements and their local persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from route_analysis.errors import StorageError
from route_analysis.models import PosePoint, VehicleDimensions
from route_analysis.storage import _atomic_json_write, _read_json
from route_analysis.turn_radius import TurnRadiusSection, calculate_turn_radius


class MeasurementSource(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class MeasurementScope:
    server_id: str
    tenant: str
    order_id: int
    task_id: int
    command_id: int
    path_name: str

    def __post_init__(self) -> None:
        if not self.server_id or not self.tenant:
            raise ValueError("measurement server and tenant must not be empty")
        if self.path_name not in {"dispatched", "actual"}:
            raise ValueError("measurement path must be dispatched or actual")

    @property
    def key(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "serverId": self.server_id,
            "tenant": self.tenant,
            "orderId": self.order_id,
            "taskId": self.task_id,
            "commandId": self.command_id,
            "pathName": self.path_name,
        }


@dataclass(frozen=True, slots=True)
class RadiusMeasurementRecord:
    id: str
    name: str
    source: MeasurementSource
    start_index: int
    end_index: int
    created_order: int

    def __post_init__(self) -> None:
        if not self.id or not self.name.strip():
            raise ValueError("measurement id and name must not be empty")
        if self.start_index < 0 or self.end_index <= self.start_index:
            raise ValueError("measurement endpoints must be ordered")


@dataclass(frozen=True, slots=True)
class CalculatedMeasurement:
    record: RadiusMeasurementRecord
    radius: TurnRadiusSection


@dataclass(slots=True)
class RadiusMeasurementState:
    path_fingerprint: str
    automatic_counter: int = 0
    manual_counter: int = 0
    _records: list[RadiusMeasurementRecord] = field(default_factory=list)

    @property
    def automatic_records(self) -> tuple[RadiusMeasurementRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self._records
                    if record.source is MeasurementSource.AUTOMATIC
                ),
                key=lambda record: (record.start_index, record.end_index, record.created_order),
            )
        )

    @property
    def manual_records(self) -> tuple[RadiusMeasurementRecord, ...]:
        return tuple(
            sorted(
                (record for record in self._records if record.source is MeasurementSource.MANUAL),
                key=lambda record: record.created_order,
            )
        )

    @property
    def records(self) -> tuple[RadiusMeasurementRecord, ...]:
        return self.automatic_records + self.manual_records

    def _next_default_name(self, source: MeasurementSource) -> tuple[str, int]:
        used = {record.name for record in self._records}
        while True:
            if source is MeasurementSource.AUTOMATIC:
                self.automatic_counter += 1
                counter = self.automatic_counter
                prefix = "自动半径"
            else:
                self.manual_counter += 1
                counter = self.manual_counter
                prefix = "手动半径"
            name = f"{prefix} {counter}"
            if name not in used:
                return name, counter

    def replace_automatic(
        self, endpoint_pairs: Iterable[tuple[int, int]]
    ) -> tuple[RadiusMeasurementRecord, ...]:
        existing = {
            (record.start_index, record.end_index): record
            for record in self._records
            if record.source is MeasurementSource.AUTOMATIC
        }
        manual = [
            record for record in self._records if record.source is MeasurementSource.MANUAL
        ]
        automatic: list[RadiusMeasurementRecord] = []
        seen: set[tuple[int, int]] = set()
        for start_index, end_index in endpoint_pairs:
            pair = (start_index, end_index)
            if pair in seen:
                continue
            seen.add(pair)
            if start_index < 0 or end_index <= start_index:
                raise ValueError("measurement endpoints must be ordered")
            record = existing.get(pair)
            if record is None:
                name, order = self._next_default_name(MeasurementSource.AUTOMATIC)
                record = RadiusMeasurementRecord(
                    uuid4().hex,
                    name,
                    MeasurementSource.AUTOMATIC,
                    start_index,
                    end_index,
                    order,
                )
            automatic.append(record)
        self._records = manual + automatic
        return self.automatic_records

    def add_manual(
        self, start_index: int, end_index: int
    ) -> tuple[RadiusMeasurementRecord, bool]:
        if start_index < 0 or end_index <= start_index:
            raise ValueError("入弯样本必须位于出弯样本之前")
        duplicate = next(
            (
                record
                for record in self._records
                if record.source is MeasurementSource.MANUAL
                and record.start_index == start_index
                and record.end_index == end_index
            ),
            None,
        )
        if duplicate is not None:
            return duplicate, False
        name, order = self._next_default_name(MeasurementSource.MANUAL)
        record = RadiusMeasurementRecord(
            uuid4().hex,
            name,
            MeasurementSource.MANUAL,
            start_index,
            end_index,
            order,
        )
        self._records.append(record)
        return record, True

    def rename(self, measurement_id: str, name: str) -> RadiusMeasurementRecord:
        normalized = name.strip()
        if not normalized:
            raise ValueError("测量名称不能为空")
        if any(
            record.id != measurement_id and record.name == normalized
            for record in self._records
        ):
            raise ValueError(f"测量名称已存在：{normalized}")
        for index, record in enumerate(self._records):
            if record.id != measurement_id:
                continue
            renamed = RadiusMeasurementRecord(
                record.id,
                normalized,
                record.source,
                record.start_index,
                record.end_index,
                record.created_order,
            )
            self._records[index] = renamed
            return renamed
        raise KeyError(f"measurement not found: {measurement_id}")

    def delete(self, measurement_id: str) -> bool:
        before = len(self._records)
        self._records = [record for record in self._records if record.id != measurement_id]
        return len(self._records) != before

    def clear_automatic(self) -> None:
        self._records = [
            record for record in self._records if record.source is MeasurementSource.MANUAL
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "pathFingerprint": self.path_fingerprint,
            "automaticCounter": self.automatic_counter,
            "manualCounter": self.manual_counter,
            "records": [
                {
                    "id": record.id,
                    "name": record.name,
                    "source": record.source.value,
                    "startIndex": record.start_index,
                    "endIndex": record.end_index,
                    "createdOrder": record.created_order,
                }
                for record in self.records
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RadiusMeasurementState:
        try:
            raw_records = payload.get("records", [])
            if not isinstance(raw_records, list):
                raise ValueError("records must be a list")
            records: list[RadiusMeasurementRecord] = []
            for raw_record in raw_records:
                if not isinstance(raw_record, Mapping):
                    raise ValueError("measurement record must be an object")
                records.append(
                    RadiusMeasurementRecord(
                        str(raw_record["id"]),
                        str(raw_record["name"]),
                        MeasurementSource(str(raw_record["source"])),
                        int(raw_record["startIndex"]),
                        int(raw_record["endIndex"]),
                        int(raw_record["createdOrder"]),
                    )
                )
            raw_automatic_counter = payload.get("automaticCounter", 0)
            raw_manual_counter = payload.get("manualCounter", 0)
            if not isinstance(raw_automatic_counter, int) or isinstance(
                raw_automatic_counter, bool
            ):
                raise ValueError("automaticCounter must be an integer")
            if not isinstance(raw_manual_counter, int) or isinstance(
                raw_manual_counter, bool
            ):
                raise ValueError("manualCounter must be an integer")
            return cls(
                path_fingerprint=str(payload["pathFingerprint"]),
                automatic_counter=raw_automatic_counter,
                manual_counter=raw_manual_counter,
                _records=records,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError("转弯半径测量文件包含无效数据") from exc


def path_fingerprint(points: Sequence[PosePoint]) -> str:
    serialized = [
        [
            float(point.x).hex(),
            float(point.y).hex(),
            None if point.yaw is None else float(point.yaw).hex(),
        ]
        for point in points
    ]
    encoded = json.dumps(serialized, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def recalculate_measurements(
    state: RadiusMeasurementState,
    points: Sequence[PosePoint],
    dimensions: VehicleDimensions,
) -> tuple[CalculatedMeasurement, ...]:
    return tuple(
        CalculatedMeasurement(
            record,
            calculate_turn_radius(
                points,
                dimensions,
                start_index=record.start_index,
                end_index=record.end_index,
            ),
        )
        for record in state.records
    )


class RadiusMeasurementRepository:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "turn-radius-measurements.json"

    def load(self, scope: MeasurementScope, fingerprint: str) -> RadiusMeasurementState:
        if not self.path.exists():
            return RadiusMeasurementState(fingerprint)
        payload = _read_json(self.path)
        entries = payload.get("entries", {})
        if not isinstance(entries, Mapping):
            raise StorageError("转弯半径测量 entries 必须是对象")
        raw_entry = entries.get(scope.key)
        if raw_entry is None:
            return RadiusMeasurementState(fingerprint)
        if not isinstance(raw_entry, Mapping):
            raise StorageError("转弯半径测量条目必须是对象")
        raw_state = raw_entry.get("state")
        if not isinstance(raw_state, Mapping):
            raise StorageError("转弯半径测量 state 必须是对象")
        state = RadiusMeasurementState.from_dict(raw_state)
        if state.path_fingerprint == fingerprint:
            return state
        return RadiusMeasurementState(fingerprint)

    def save(self, scope: MeasurementScope, state: RadiusMeasurementState) -> Path:
        payload: Mapping[str, object] = _read_json(self.path) if self.path.exists() else {}
        raw_entries = payload.get("entries", {})
        if not isinstance(raw_entries, Mapping):
            raise StorageError("转弯半径测量 entries 必须是对象")
        entries = dict(raw_entries)
        entries[scope.key] = {"scope": scope.to_dict(), "state": state.to_dict()}
        _atomic_json_write(
            self.path,
            {"schemaVersion": 1, "entries": entries},
            backup=True,
        )
        return self.path
