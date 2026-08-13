import json
from pathlib import Path

import pytest

from route_analysis.errors import StorageError
from route_analysis.models import JoinStyle, Lane, Point2D, VehicleDimensions
from route_analysis.storage import (
    AppConfig,
    ConfigRepository,
    ImportMismatchError,
    LaneLayout,
    LaneRepository,
    VehicleProfileRepository,
    server_id_for,
)


def test_config_defaults_tenant_and_round_trips_plaintext_password(tmp_path: Path) -> None:
    repository = ConfigRepository(tmp_path)
    config = AppConfig()
    config.api_root = "http://example.test/admin-api"
    config.username = "operator"
    config.password = "plain-secret"
    config.default_vehicle = VehicleDimensions(1.2, 0.8, 1.4)
    config.default_lane_width = 2.5

    repository.save(config)
    loaded = repository.load()
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")

    assert loaded.tenant == "suntae"
    assert loaded.default_vehicle == VehicleDimensions(1.2, 0.8, 1.4)
    assert "plain-secret" in raw
    assert "accessToken" not in raw
    assert loaded.first_run_complete


def test_vehicle_profiles_use_vin_override_or_global_default(tmp_path: Path) -> None:
    repository = VehicleProfileRepository(tmp_path)
    default = VehicleDimensions(1, 1, 1)
    override = VehicleDimensions(2, 2, 2)

    repository.save({"VIN-2": override})
    profiles = repository.load()

    assert profiles.resolve("VIN-2", default) == override
    assert profiles.resolve("VIN-1", default) == default


def test_lane_layout_is_keyed_by_server_and_map_and_keeps_previous_backup(tmp_path: Path) -> None:
    repository = LaneRepository(tmp_path)
    first = Lane.create("one", "One", 2, [Point2D(0, 0), Point2D(2, 0)])
    server_id = server_id_for("HTTP://Example.Test/admin-api/")
    layout = LaneLayout(server_id=server_id, map_id="42", lanes=[first])

    path = repository.save(layout)
    layout.lanes[0].width = 3
    repository.save(layout)

    assert path == tmp_path / "lanes" / server_id / "42.json"
    assert path.with_suffix(".json.bak").exists()
    assert repository.load(server_id, "42").lanes[0].width == 3
    assert repository.load(server_id, "43").lanes == []


def test_import_is_previewed_and_replaces_instead_of_merging(tmp_path: Path) -> None:
    repository = LaneRepository(tmp_path / "data")
    target = LaneLayout(
        server_id="aaaaaaaaaaaaaaaa",
        map_id="5",
        lanes=[Lane.create("old", "Old", 2, [Point2D(0, 0), Point2D(1, 0)])],
    )
    repository.save(target)
    source = LaneLayout(
        server_id="bbbbbbbbbbbbbbbb",
        map_id="9",
        lanes=[
            Lane.create(
                "new",
                "New",
                3,
                [Point2D(1, 1), Point2D(2, 2)],
                default_join=JoinStyle.ROUND,
            )
        ],
    )
    export_file = tmp_path / "import.json"
    export_file.write_text(json.dumps(source.to_dict()), encoding="utf-8")

    preview = repository.preview_import(
        export_file, expected_server_id="aaaaaaaaaaaaaaaa", expected_map_id="5"
    )

    assert set(preview.mismatches) == {"server_id", "map_id"}
    with pytest.raises(ImportMismatchError):
        repository.replace_from_import(
            export_file, "aaaaaaaaaaaaaaaa", "5", allow_mismatch=False
        )

    repository.replace_from_import(export_file, "aaaaaaaaaaaaaaaa", "5", allow_mismatch=True)
    loaded = repository.load("aaaaaaaaaaaaaaaa", "5")
    assert [lane.id for lane in loaded.lanes] == ["new"]
    assert loaded.server_id == "aaaaaaaaaaaaaaaa"
    assert loaded.map_id == "5"
    assert (tmp_path / "data" / "lanes" / "aaaaaaaaaaaaaaaa" / "5.json.bak").exists()


def test_import_rejects_unknown_units(tmp_path: Path) -> None:
    source = tmp_path / "feet.json"
    source.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "serverId": "aaaaaaaaaaaaaaaa",
                "mapId": "5",
                "units": {"distance": "ft", "angle": "degree"},
                "lanes": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StorageError, match="导入文件内容无效"):
        LaneRepository(tmp_path / "data").preview_import(
            source,
            expected_server_id="aaaaaaaaaaaaaaaa",
            expected_map_id="5",
        )
