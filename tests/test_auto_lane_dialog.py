import math

import pytest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from route_analysis.auto_lane_dialog import (
    AutoLaneDialog,
    LanePickRequest,
    run_auto_lane_dialog,
)
from route_analysis.lane_generation import BendMode, ConnectionMode, LaneGenerationResult
from route_analysis.models import PosePoint, SegmentKind, VehicleDimensions

DIMENSIONS = VehicleDimensions(width=1.20, center_front=1.00, center_rear=1.60)


def _cornering() -> tuple[PosePoint, ...]:
    poses = [PosePoint(-4 + index * 1.0, 0.0, 0.0) for index in range(5)]
    for step in range(1, 13):
        angle = -math.pi / 2 + math.pi / 2 * step / 12
        poses.append(
            PosePoint(2.0 * math.cos(angle), 2.0 + 2.0 * math.sin(angle), angle + math.pi / 2)
        )
    poses.extend(PosePoint(2.0, 2.0 + index * 1.0, math.pi / 2) for index in range(1, 5))
    return tuple(poses)


def _request(
    poses: tuple[PosePoint, ...] | None = None,
    *,
    start_index: int = 0,
    end_index: int | None = None,
) -> LanePickRequest:
    points = poses if poses is not None else _cornering()
    return LanePickRequest(
        source="dispatched",
        poses=points,
        start_index=start_index,
        end_index=len(points) - 1 if end_index is None else end_index,
        start_label=start_index + 1,
        end_label=(len(points) if end_index is None else end_index + 1),
        dimensions=DIMENSIONS,
    )


def _dialog(request: LanePickRequest, **overrides: object) -> AutoLaneDialog:
    settings: dict[str, object] = {
        "default_width": 2.5,
        "maximum_deviation": 0.05,
        "last_mode": BendMode.SHARP,
        "last_connection": ConnectionMode.PATH,
    }
    settings.update(overrides)
    return AutoLaneDialog(request, **settings)  # type: ignore[arg-type]


def test_run_dialog_keeps_parameter_window_top_level(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    observed: dict[str, object] = {}

    def inspect_dialog(dialog: AutoLaneDialog) -> AutoLaneDialog.DialogCode:
        observed["is_window"] = dialog.isWindow()
        observed["is_modal"] = dialog.isModal()
        observed["parent"] = dialog.parent()
        return AutoLaneDialog.DialogCode.Rejected

    monkeypatch.setattr(AutoLaneDialog, "exec", inspect_dialog)

    selection = run_auto_lane_dialog(
        parent,
        _request(),
        default_width=2.5,
        maximum_deviation=0.05,
        last_mode=BendMode.SHARP,
        last_connection=ConnectionMode.PATH,
        preview_callback=lambda _result: None,
    )

    assert selection is None
    assert observed == {"is_window": True, "is_modal": True, "parent": parent}


def test_dialog_names_the_lane_after_the_two_chosen_samples(qtbot: QtBot) -> None:
    dialog = _dialog(_request(start_index=2, end_index=9))
    qtbot.addWidget(dialog)
    assert "3" in dialog.name_edit.text()
    assert "10" in dialog.name_edit.text()
    assert dialog.width_spin.value() == 2.5


def test_dialog_previews_and_reports_both_overhangs(qtbot: QtBot) -> None:
    dialog = _dialog(_request())
    qtbot.addWidget(dialog)
    previews: list[LaneGenerationResult | None] = []
    dialog.preview_changed.connect(previews.append)

    dialog.refresh_preview()

    assert previews[-1] is not None
    assert previews[-1].metrics.start_overhang == pytest.approx(1.60)
    assert previews[-1].metrics.end_overhang == pytest.approx(1.00)
    assert "两端延伸" in dialog.metrics_label.text()


def test_a_straight_connection_disables_what_it_cannot_use(qtbot: QtBot) -> None:
    dialog = _dialog(_request())
    qtbot.addWidget(dialog)
    assert dialog.mode_combo.isEnabled()
    assert dialog.deviation_spin.isEnabled()
    assert dialog.closed_check.isEnabled()

    dialog.connection_combo.setCurrentIndex(
        dialog.connection_combo.findData(ConnectionMode.STRAIGHT.value)
    )

    assert dialog.connection() is ConnectionMode.STRAIGHT
    assert not dialog.mode_combo.isEnabled()
    assert not dialog.deviation_spin.isEnabled()
    assert not dialog.closed_check.isEnabled()
    assert not dialog.closed_check.isChecked()
    assert "不经过弯道拟合" in dialog.connection_note.text()


def test_a_straight_connection_produces_a_two_anchor_lane(qtbot: QtBot) -> None:
    dialog = _dialog(_request(), last_connection=ConnectionMode.STRAIGHT)
    qtbot.addWidget(dialog)
    dialog.refresh_preview()
    dialog.accept()

    assert dialog.generation_result is not None
    lane = dialog.generation_result.lane
    assert len(lane.anchors) == 2
    assert all(segment.kind is SegmentKind.LINE for segment in lane.segments)


def test_following_the_path_keeps_the_chosen_bend_mode(qtbot: QtBot) -> None:
    dialog = _dialog(_request(), last_mode=BendMode.BEZIER)
    qtbot.addWidget(dialog)
    dialog.refresh_preview()
    dialog.accept()

    assert dialog.generation_result is not None
    assert any(
        segment.kind is SegmentKind.CUBIC
        for segment in dialog.generation_result.lane.segments
    )


def test_a_lane_narrower_than_the_vehicle_is_flagged_but_still_allowed(qtbot: QtBot) -> None:
    dialog = _dialog(_request(), default_width=0.5)
    qtbot.addWidget(dialog)
    dialog.refresh_preview()

    assert dialog.generation_result is not None
    assert dialog.ok_button.isEnabled()
    assert "小于车宽" in dialog.metrics_label.text()
    assert "露出车道" in dialog.metrics_label.text()


def test_a_wide_enough_lane_is_not_flagged(qtbot: QtBot) -> None:
    dialog = _dialog(_request(), default_width=2.5)
    qtbot.addWidget(dialog)
    dialog.refresh_preview()
    assert "小于车宽" not in dialog.metrics_label.text()


def test_dialog_refuses_a_pick_it_cannot_build_without_touching_any_layout(
    qtbot: QtBot,
) -> None:
    poses = (PosePoint(0, 0, 0.0), PosePoint(1, 0, None), PosePoint(2, 0, 0.0))
    dialog = _dialog(_request(poses, start_index=0, end_index=1))
    qtbot.addWidget(dialog)
    previews: list[LaneGenerationResult | None] = []
    dialog.preview_changed.connect(previews.append)

    dialog.refresh_preview()

    assert previews[-1] is None
    assert "缺少 yaw" in dialog.metrics_label.text()
    assert dialog.ok_button.isEnabled() is False
