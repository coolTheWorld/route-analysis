import json

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from route_analysis.parsing import parse_command_details
from route_analysis.path_details_panel import PathDetailsPanel, PathPointTableModel


def command_details(prefix: str = "dispatched"):
    return parse_command_details(
        {
            "commandId": 9063,
            "source": prefix,
            "positionList": [
                {"x": 1, "y": 2, "yaw": 0.25, "gear": "D", "speed": 0.5},
                {"x": 3.5, "y": -4, "yaw": None, "gear": "R", "extra": {"a": 1}},
                {"x": "invalid", "y": 6, "yaw": 0.75},
            ],
        }
    )


def test_point_table_model_formats_columns_and_marks_invalid_rows() -> None:
    model = PathPointTableModel(command_details().points)

    assert model.rowCount() == 3
    assert model.columnCount() == 5
    assert [
        model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for column in range(5)
    ] == ["序号", "X", "Y", "Yaw（rad）", "Gear"]
    assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole) == "D"
    assert model.data(model.index(1, 3), Qt.ItemDataRole.DisplayRole) == "—"
    assert model.data(model.index(2, 0), Qt.ItemDataRole.DisplayRole) == "3 ⚠"
    assert model.data(model.index(2, 1), Qt.ItemDataRole.DisplayRole) == "—"
    assert "不是有效数字" in model.data(
        model.index(2, 0), Qt.ItemDataRole.ToolTipRole
    )


def test_panel_switches_command_and_point_json_and_remembers_each_source_selection(
    qtbot: QtBot,
) -> None:
    panel = PathDetailsPanel()
    qtbot.addWidget(panel)
    panel.begin_command()
    panel.set_source_document("dispatched", command_details("dispatched"))
    panel.set_source_document("actual", command_details("actual"))

    assert panel.current_path_name == "dispatched"
    assert panel.json_tabs.currentIndex() == 0
    command_json = json.loads(panel.command_json_editor.toPlainText())
    assert command_json["source"] == "dispatched"

    with qtbot.waitSignal(panel.point_selected, timeout=1000) as selected:
        panel.tables["dispatched"].selectRow(1)

    assert selected.args == ["dispatched", 1]
    assert panel.json_tabs.currentIndex() == 1
    assert json.loads(panel.point_json_editor.toPlainText()) == {
        "x": 3.5,
        "y": -4,
        "yaw": None,
        "gear": "R",
        "extra": {"a": 1},
    }

    panel.source_tabs.setCurrentIndex(1)
    panel.tables["actual"].selectRow(0)
    panel.source_tabs.setCurrentIndex(0)

    assert panel.tables["dispatched"].currentIndex().row() == 1
    assert json.loads(panel.point_json_editor.toPlainText())["gear"] == "R"


def test_jump_uses_one_based_sequence_and_scrolls_to_the_row(qtbot: QtBot) -> None:
    panel = PathDetailsPanel()
    qtbot.addWidget(panel)
    panel.begin_command()
    panel.set_source_document("dispatched", command_details())
    panel.jump_spins["dispatched"].setValue(3)

    with qtbot.waitSignal(panel.point_selected, timeout=1000) as selected:
        panel.jump_buttons["dispatched"].click()

    assert selected.args == ["dispatched", 2]
    assert panel.tables["dispatched"].currentIndex().row() == 2


def test_error_state_retries_only_the_failed_source(qtbot: QtBot) -> None:
    panel = PathDetailsPanel()
    qtbot.addWidget(panel)
    panel.begin_command()
    panel.set_source_document("dispatched", command_details())
    panel.set_source_error("actual", "后端暂时不可用")

    assert panel.models["dispatched"].rowCount() == 3
    panel.source_tabs.setCurrentIndex(1)
    assert not panel.retry_buttons["actual"].isHidden()
    assert "后端暂时不可用" in panel.status_labels["actual"].text()

    with qtbot.waitSignal(panel.retry_requested, timeout=1000) as retried:
        panel.retry_buttons["actual"].click()

    assert retried.args == ["actual"]
