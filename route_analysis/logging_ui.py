"""Qt presentation helpers for local log state and file actions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QMenuBar, QWidget


def add_log_menu(menu_bar: QMenuBar, parent: QWidget, log_file: Path) -> None:
    help_menu = menu_bar.addMenu("帮助")
    open_directory = QAction("打开日志目录", parent)
    open_directory.triggered.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file.parent)))
    )
    open_current = QAction("打开当前日志", parent)
    open_current.triggered.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file)))
    )
    help_menu.addActions([open_directory, open_current])
