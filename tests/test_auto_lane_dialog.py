import pytest
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from route_analysis.auto_lane_dialog import AutoLaneDialog, run_auto_lane_dialog
from route_analysis.lane_generation import BendMode, LaneGenerationResult
from route_analysis.models import PosePoint, SegmentKind


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
        {
            "dispatched": (PosePoint(0, 0, None), PosePoint(1, 0, None)),
            "actual": (),
        },
        default_width=2.5,
        maximum_deviation=0.05,
        last_mode=BendMode.SHARP,
        preview_callback=lambda _result: None,
    )

    assert selection is None
    assert observed == {
        "is_window": True,
        "is_modal": True,
        "parent": parent,
    }


def test_dialog_defaults_to_dispatched_and_previews_generation(qtbot: QtBot) -> None:
    dialog = AutoLaneDialog(
        {
            "dispatched": (
                PosePoint(0, 0, None),
                PosePoint(1, 0.01, None),
                PosePoint(2, 0, None),
            ),
            "actual": (PosePoint(0, 1, 0), PosePoint(2, 1, 0)),
        },
        default_width=2.5,
        maximum_deviation=0.05,
        last_mode=BendMode.SHARP,
    )
    qtbot.addWidget(dialog)
    previews: list[LaneGenerationResult | None] = []
    dialog.preview_changed.connect(previews.append)

    dialog.refresh_preview()

    assert dialog.source_combo.currentData() == "dispatched"
    assert dialog.width_spin.value() == 2.5
    assert dialog.closed_check.isChecked() is False
    assert previews[-1] is not None
    assert previews[-1].metrics.anchors == 2
    assert "最大偏差" in dialog.metrics_label.text()


def test_dialog_auto_checks_near_closed_path_but_user_controls_final_state(
    qtbot: QtBot,
) -> None:
    path = (
        PosePoint(0, 0, None),
        PosePoint(2, 0, None),
        PosePoint(2, 2, None),
        PosePoint(0.01, 0.01, None),
    )
    dialog = AutoLaneDialog(
        {"dispatched": path, "actual": ()},
        default_width=2,
        maximum_deviation=0.05,
        last_mode=BendMode.BEZIER,
    )
    qtbot.addWidget(dialog)

    assert dialog.closed_check.isChecked() is True
    dialog.closed_check.setChecked(False)
    dialog.refresh_preview()
    dialog.accept()

    assert dialog.generation_result is not None
    assert dialog.generation_result.lane.closed is False
    assert all(
        segment.kind is SegmentKind.CUBIC
        for segment in dialog.generation_result.lane.segments
    )


def test_dialog_reports_invalid_source_without_modifying_any_layout(qtbot: QtBot) -> None:
    dialog = AutoLaneDialog(
        {"dispatched": (PosePoint(1, 1, None),), "actual": ()},
        default_width=2,
        maximum_deviation=0.05,
        last_mode=BendMode.SHARP,
    )
    qtbot.addWidget(dialog)
    previews: list[LaneGenerationResult | None] = []
    dialog.preview_changed.connect(previews.append)

    dialog.refresh_preview()

    assert previews[-1] is None
    assert "至少" in dialog.metrics_label.text()
    assert dialog.ok_button.isEnabled() is False
