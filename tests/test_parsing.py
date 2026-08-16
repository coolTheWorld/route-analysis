import json

import pytest

from route_analysis.errors import DataContractError
from route_analysis.models import PosePoint
from route_analysis.parsing import parse_command_details, parse_command_path


def test_command_path_parser_uses_yaw_and_keeps_missing_yaw() -> None:
    payload = json.dumps(
        {
            "positionList": [
                {"x": 1, "y": 2, "yaw": 0.5, "roadYaw": 1.5},
                {"x": "3.25", "y": "-4", "yaw": None},
            ]
        }
    )

    points = parse_command_path(payload)

    assert [(point.x, point.y, point.yaw) for point in points] == [
        (1, 2, 0.5),
        (3.25, -4, None),
    ]


def test_command_path_parser_does_not_infer_yaw_from_road_yaw() -> None:
    points = parse_command_path({"positionList": [{"x": 1, "y": 2, "roadYaw": 1.5}]})

    assert points[0].yaw is None


def test_command_details_preserve_complete_json_gear_and_source_order() -> None:
    payload = json.dumps(
        {
            "commandId": 9063,
            "positionList": [
                {"x": 1, "y": 2, "yaw": 0.5, "gear": "D", "speed": 0.8},
                {"x": "3.25", "y": "-4", "yaw": None, "gear": "R"},
            ],
            "metadata": {"source": "scheduler"},
        }
    )

    details = parse_command_details(payload)

    assert list(details.raw_command or {}) == ["commandId", "positionList", "metadata"]
    assert details.points[0].raw == {
        "x": 1,
        "y": 2,
        "yaw": 0.5,
        "gear": "D",
        "speed": 0.8,
    }
    assert details.points[0].gear == "D"
    assert details.points[1].gear == "R"
    assert details.poses == (PosePoint(1, 2, 0.5), PosePoint(3.25, -4, None))
    assert details.pose_source_indices == (0, 1)


def test_command_details_keep_invalid_rows_but_exclude_invalid_coordinates_from_poses() -> None:
    details = parse_command_details(
        {
            "positionList": [
                {"x": "invalid", "y": 2, "gear": "R"},
                {"x": 3, "y": 4, "yaw": "invalid", "gear": "D"},
                "invalid point object",
            ]
        }
    )

    assert len(details.points) == 3
    assert details.points[0].x is None
    assert details.points[0].errors
    assert details.points[1].pose == PosePoint(3, 4, None)
    assert details.points[1].errors
    assert details.points[2].pose is None
    assert details.poses == (PosePoint(3, 4, None),)
    assert details.pose_source_indices == (1,)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"positionList": "not-a-list"},
        {"positionList": [{"x": None, "y": 2}]},
        {"positionList": [{"x": float("inf"), "y": 2}]},
    ],
)
def test_command_path_parser_rejects_invalid_contract(payload: object) -> None:
    with pytest.raises(DataContractError):
        parse_command_path(payload)
