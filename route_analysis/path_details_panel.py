"""Inspectable command path tables and read-only raw JSON details."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import ClassVar, cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from route_analysis.models import CommandPathData, PathPointData

_INVALID_INDEX = QModelIndex()
ModelIndex = QModelIndex | QPersistentModelIndex


def _display_number(value: float | None) -> str:
    return "—" if value is None else format(value, ".12g")


def _display_json_value(value: object | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _formatted_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


class PathPointTableModel(QAbstractTableModel):
    """A lazy table projection over immutable path-point data."""

    HEADERS = ("序号", "X", "Y", "Yaw（rad）", "Gear")

    def __init__(self, points: Iterable[PathPointData] = ()) -> None:
        super().__init__()
        self._points = tuple(points)

    @property
    def points(self) -> tuple[PathPointData, ...]:
        return self._points

    def set_points(self, points: Iterable[PathPointData]) -> None:
        self.beginResetModel()
        self._points = tuple(points)
        self.endResetModel()

    def point_at(self, row: int) -> PathPointData | None:
        if not 0 <= row < len(self._points):
            return None
        return self._points[row]

    def row_for_source_index(self, source_index: int) -> int | None:
        if 0 <= source_index < len(self._points):
            point = self._points[source_index]
            if point.source_index == source_index:
                return source_index
        return next(
            (row for row, point in enumerate(self._points) if point.source_index == source_index),
            None,
        )

    def rowCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._points)

    def columnCount(self, parent: ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return cast(object | None, super().headerData(section, orientation, role))

    def data(self, index: ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object | None:
        if not index.isValid() or not 0 <= index.row() < len(self._points):
            return None
        point = self._points[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                f"{point.source_index + 1}{' ⚠' if point.errors else ''}",
                _display_number(point.x),
                _display_number(point.y),
                _display_number(point.yaw),
                _display_json_value(point.gear),
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and point.errors:
            return "数据异常：" + "；".join(point.errors)
        if role == Qt.ItemDataRole.ForegroundRole and point.errors:
            return QColor("#a10f2b")
        if role == Qt.ItemDataRole.BackgroundRole and point.errors:
            return QColor("#fff1f3")
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        return None


class _PathSourcePage(QWidget):
    def __init__(self, name: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        state_row = QHBoxLayout()
        self.status_label = QLabel("尚未加载")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName(f"{name}点位状态")
        self.retry_button = QPushButton("重试")
        self.retry_button.setVisible(False)
        self.retry_button.setAccessibleName(f"重试加载{name}点位")
        state_row.addWidget(self.status_label, 1)
        state_row.addWidget(self.retry_button)
        layout.addLayout(state_row)

        self.model = PathPointTableModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName(f"{name}点位表格")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        jump_row = QHBoxLayout()
        jump_row.addWidget(QLabel("跳转到序号"))
        self.jump_spin = QSpinBox()
        self.jump_spin.setRange(1, 1)
        self.jump_spin.setEnabled(False)
        self.jump_spin.setAccessibleName(f"{name}点位跳转序号")
        self.jump_button = QPushButton("跳转")
        self.jump_button.setEnabled(False)
        self.jump_button.setAccessibleName(f"跳转到{name}点位序号")
        jump_row.addWidget(self.jump_spin, 1)
        jump_row.addWidget(self.jump_button)
        layout.addLayout(jump_row)

    def set_document(self, document: CommandPathData) -> None:
        self.model.set_points(document.points)
        count = len(document.points)
        self.status_label.setText(f"共 {count} 个点位" if count else "无点位数据")
        self.retry_button.setVisible(False)
        self.jump_spin.setRange(1, max(1, count))
        self.jump_spin.setEnabled(count > 0)
        self.jump_button.setEnabled(count > 0)

    def clear(self) -> None:
        self.model.set_points(())
        self.table.clearSelection()
        self.table.setCurrentIndex(QModelIndex())
        self.status_label.setText("尚未加载")
        self.retry_button.setVisible(False)
        self.jump_spin.setRange(1, 1)
        self.jump_spin.setEnabled(False)
        self.jump_button.setEnabled(False)


class PathDetailsPanel(QFrame):
    """Two path-source tables synchronized with command and point JSON views."""

    point_selected = Signal(str, int)
    active_path_changed = Signal(str)
    retry_requested = Signal(str)

    SOURCE_ORDER: ClassVar[tuple[str, str]] = ("dispatched", "actual")
    SOURCE_LABELS: ClassVar[dict[str, str]] = {"dispatched": "下发", "actual": "实际"}

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pathDetailsPanel")
        self.setAccessibleName("命令点位详情")
        self._documents = {name: CommandPathData.empty() for name in self.SOURCE_ORDER}
        self._selected_indices: dict[str, int | None] = {
            name: None for name in self.SOURCE_ORDER
        }
        self._suppress_selection_signal = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)

        self.source_tabs = QTabWidget()
        self.source_tabs.setAccessibleName("路径点位来源")
        self._pages: dict[str, _PathSourcePage] = {}
        self.tables: dict[str, QTableView] = {}
        self.models: dict[str, PathPointTableModel] = {}
        self.status_labels: dict[str, QLabel] = {}
        self.retry_buttons: dict[str, QPushButton] = {}
        self.jump_spins: dict[str, QSpinBox] = {}
        self.jump_buttons: dict[str, QPushButton] = {}
        for name in self.SOURCE_ORDER:
            label = self.SOURCE_LABELS[name]
            page = _PathSourcePage(label)
            self._pages[name] = page
            self.tables[name] = page.table
            self.models[name] = page.model
            self.status_labels[name] = page.status_label
            self.retry_buttons[name] = page.retry_button
            self.jump_spins[name] = page.jump_spin
            self.jump_buttons[name] = page.jump_button
            self.source_tabs.addTab(page, f"{label}点位")
            page.table.selectionModel().currentRowChanged.connect(
                lambda current, _previous, path_name=name: self._row_changed(
                    path_name, current.row()
                )
            )
            page.jump_button.clicked.connect(
                lambda _checked=False, path_name=name: self._jump(path_name)
            )
            page.jump_spin.lineEdit().returnPressed.connect(
                lambda path_name=name: self._jump(path_name)
            )
            page.retry_button.clicked.connect(
                lambda _checked=False, path_name=name: self.retry_requested.emit(path_name)
            )
        self.source_tabs.currentChanged.connect(self._source_changed)
        self.splitter.addWidget(self.source_tabs)

        self.json_tabs = QTabWidget()
        self.json_tabs.setAccessibleName("JSON 详情类型")
        self.command_json_editor = self._json_editor("命令完整 JSON")
        self.point_json_editor = self._json_editor("选中点位完整 JSON")
        self.json_tabs.addTab(self.command_json_editor, "命令 JSON")
        self.json_tabs.addTab(self.point_json_editor, "点位 JSON")
        self.splitter.addWidget(self.json_tabs)
        self.splitter.setSizes([360, 240])
        layout.addWidget(self.splitter)

    @staticmethod
    def _json_editor(accessible_name: str) -> QPlainTextEdit:
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        editor.setAccessibleName(accessible_name)
        return editor

    @property
    def current_path_name(self) -> str:
        index = max(0, self.source_tabs.currentIndex())
        return self.SOURCE_ORDER[index]

    def begin_command(self) -> None:
        self._documents = {name: CommandPathData.empty() for name in self.SOURCE_ORDER}
        self._selected_indices = {name: None for name in self.SOURCE_ORDER}
        for page in self._pages.values():
            page.clear()
        self.source_tabs.setCurrentIndex(0)
        self.json_tabs.setCurrentIndex(0)
        self.command_json_editor.setPlainText("正在加载下发命令 JSON…")
        self.point_json_editor.setPlainText("尚未选择点位")

    def set_source_loading(self, path_name: str) -> None:
        page = self._page(path_name)
        page.status_label.setText(f"正在加载{self.SOURCE_LABELS[path_name]}点位…")
        page.retry_button.setVisible(False)
        if path_name == self.current_path_name:
            self._show_current_json()

    def set_source_document(self, path_name: str, document: CommandPathData) -> None:
        page = self._page(path_name)
        self._documents[path_name] = document
        page.set_document(document)
        selected = self._selected_indices[path_name]
        if selected is not None and page.model.row_for_source_index(selected) is not None:
            self.select_point(path_name, selected, emit_signal=False)
        else:
            self._selected_indices[path_name] = None
        if path_name == self.current_path_name:
            self._show_current_json()

    def set_source_error(self, path_name: str, message: str) -> None:
        page = self._page(path_name)
        page.status_label.setText(f"加载失败：{message}")
        page.retry_button.setVisible(True)
        if path_name == self.current_path_name and not page.model.points:
            self.command_json_editor.setPlainText(f"{self.SOURCE_LABELS[path_name]}路径加载失败")

    def selected_source_index(self, path_name: str) -> int | None:
        self._page(path_name)
        return self._selected_indices[path_name]

    def select_point(
        self,
        path_name: str,
        source_index: int,
        *,
        emit_signal: bool = False,
    ) -> bool:
        page = self._page(path_name)
        row = page.model.row_for_source_index(source_index)
        if row is None:
            return False
        tab_index = self.SOURCE_ORDER.index(path_name)
        if self.source_tabs.currentIndex() != tab_index:
            self.source_tabs.setCurrentIndex(tab_index)
        self._suppress_selection_signal = True
        try:
            index = page.model.index(row, 0)
            page.table.setCurrentIndex(index)
            page.table.selectRow(row)
            page.table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        finally:
            self._suppress_selection_signal = False
        self._selected_indices[path_name] = source_index
        self._show_point_json(path_name, source_index)
        if emit_signal:
            self.point_selected.emit(path_name, source_index)
        return True

    def _page(self, path_name: str) -> _PathSourcePage:
        try:
            return self._pages[path_name]
        except KeyError as exc:
            raise ValueError(f"unknown path: {path_name}") from exc

    def _row_changed(self, path_name: str, row: int) -> None:
        point = self._pages[path_name].model.point_at(row)
        if point is None:
            return
        self._selected_indices[path_name] = point.source_index
        if path_name == self.current_path_name:
            self._show_point_json(path_name, point.source_index)
        if not self._suppress_selection_signal:
            self.point_selected.emit(path_name, point.source_index)

    def _jump(self, path_name: str) -> None:
        source_index = self._pages[path_name].jump_spin.value() - 1
        self.select_point(path_name, source_index, emit_signal=True)

    def _source_changed(self, index: int) -> None:
        if not 0 <= index < len(self.SOURCE_ORDER):
            return
        path_name = self.SOURCE_ORDER[index]
        self._show_current_json()
        self.active_path_changed.emit(path_name)

    def _show_current_json(self) -> None:
        path_name = self.current_path_name
        selected = self._selected_indices[path_name]
        if selected is None:
            self._show_command_json(path_name)
            return
        self._show_point_json(path_name, selected)

    def _show_command_json(self, path_name: str) -> None:
        command = self._documents[path_name].raw_command
        if command is None:
            text = (
                "无实际路径数据"
                if path_name == "actual"
                else "无下发路径数据"
            )
        else:
            text = _formatted_json(command)
        self.command_json_editor.setPlainText(text)
        self.json_tabs.setCurrentIndex(0)

    def _show_point_json(self, path_name: str, source_index: int) -> None:
        model = self._pages[path_name].model
        row = model.row_for_source_index(source_index)
        point = None if row is None else model.point_at(row)
        if point is None:
            self.point_json_editor.setPlainText("尚未选择点位")
            return
        self.point_json_editor.setPlainText(_formatted_json(point.raw))
        self.json_tabs.setCurrentIndex(1)
