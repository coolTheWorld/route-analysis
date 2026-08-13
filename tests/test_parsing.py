import json

import pytest

from route_analysis.errors import DataContractError
from route_analysis.parsing import parse_command_path


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
