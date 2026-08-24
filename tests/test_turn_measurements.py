from pathlib import Path

import pytest

from route_analysis.models import PosePoint, VehicleDimensions
from route_analysis.turn_measurements import (
    LEGACY_AUTOMATIC_SOURCE,
    MeasurementScope,
    RadiusMeasurementRepository,
    RadiusMeasurementState,
    path_fingerprint,
    recalculate_measurements,
)


def _path() -> tuple[PosePoint, ...]:
    return (
        PosePoint(5, 0, 0.5 * 3.141592653589793),
        PosePoint(3.5355339, 3.5355339, 0.75 * 3.141592653589793),
        PosePoint(0, 5, 3.141592653589793),
    )


def test_path_fingerprint_includes_order_coordinates_and_yaw() -> None:
    points = _path()
    assert path_fingerprint(points) == path_fingerprint(tuple(points))
    assert path_fingerprint(tuple(reversed(points))) != path_fingerprint(points)
    changed_yaw = (*points[:-1], PosePoint(points[-1].x, points[-1].y, 3.0))
    assert path_fingerprint(changed_yaw) != path_fingerprint(points)


def test_legacy_automatic_records_are_dropped_when_reading_an_old_file() -> None:
    payload = {
        "pathFingerprint": "fingerprint",
        "automaticCounter": 2,
        "manualCounter": 1,
        "records": [
            {
                "id": "a",
                "name": "自动半径 1",
                "source": LEGACY_AUTOMATIC_SOURCE,
                "startIndex": 0,
                "endIndex": 2,
                "createdOrder": 1,
            },
            {
                "id": "b",
                "name": "手动半径 1",
                "source": "manual",
                "startIndex": 1,
                "endIndex": 3,
                "createdOrder": 1,
            },
        ],
    }

    state = RadiusMeasurementState.from_dict(payload)

    assert [record.name for record in state.records] == ["手动半径 1"]
    assert state.manual_counter == 1
    assert "automaticCounter" not in state.to_dict()
    assert "source" not in state.to_dict()["records"][0]


def test_manual_duplicate_is_located_and_names_are_unique() -> None:
    state = RadiusMeasurementState(path_fingerprint="fingerprint")
    manual, created = state.add_manual(1, 3)
    duplicate, duplicate_created = state.add_manual(1, 3)

    assert created is True
    assert duplicate_created is False
    assert duplicate == manual
    second, _ = state.add_manual(2, 4)
    with pytest.raises(ValueError, match="名称已存在"):
        state.rename(second.id, manual.name)
    with pytest.raises(ValueError, match="不能为空"):
        state.rename(second.id, "  ")


def test_deleted_names_are_not_reused_and_missing_ids_report_no_deletion() -> None:
    state = RadiusMeasurementState(path_fingerprint="fingerprint")
    first_manual, _created = state.add_manual(1, 3)

    assert state.delete(first_manual.id) is True
    second_manual, _created = state.add_manual(2, 4)
    assert second_manual.name == "手动半径 2"

    assert state.records == (second_manual,)
    assert state.delete(first_manual.id) is False


def test_repository_isolates_scope_and_invalidates_changed_paths(tmp_path: Path) -> None:
    repository = RadiusMeasurementRepository(tmp_path)
    points = _path()
    fingerprint = path_fingerprint(points)
    first_scope = MeasurementScope("server", "suntae", 1, 2, 3, "dispatched")
    other_scope = MeasurementScope("server", "other", 1, 2, 3, "dispatched")
    state = RadiusMeasurementState(path_fingerprint=fingerprint)
    state.add_manual(0, 2)
    repository.save(first_scope, state)

    loaded = repository.load(first_scope, fingerprint)
    isolated = repository.load(other_scope, fingerprint)
    invalidated = repository.load(first_scope, path_fingerprint(points[:-1]))

    assert len(loaded.records) == 1
    assert isolated.records == ()
    assert invalidated.records == ()


def test_recalculation_keeps_the_record_when_the_radius_cannot_be_calculated() -> None:
    points = (PosePoint(0, 0, 0), PosePoint(0, 0, 0.4), PosePoint(0, 0, 0.8))
    state = RadiusMeasurementState(path_fingerprint=path_fingerprint(points))
    state.add_manual(0, 2)

    measurements = recalculate_measurements(
        state,
        points,
        VehicleDimensions(width=2, center_front=3, center_rear=1),
    )

    assert len(measurements) == 1
    assert measurements[0].radius.valid is False
    assert measurements[0].radius.error


def test_dimension_change_recomputes_corners_but_not_front_axle_radius() -> None:
    points = _path()
    state = RadiusMeasurementState(path_fingerprint=path_fingerprint(points))
    state.add_manual(0, 2)

    compact = recalculate_measurements(
        state,
        points,
        VehicleDimensions(width=1, center_front=1, center_rear=1),
    )[0].radius
    large = recalculate_measurements(
        state,
        points,
        VehicleDimensions(width=3, center_front=4, center_rear=2),
    )[0].radius

    assert compact.front_axle_radius == pytest.approx(large.front_axle_radius)
    assert compact.radii != large.radii
