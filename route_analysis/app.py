"""Application entry point and first-run gate."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from route_analysis import __version__
from route_analysis.errors import RouteAnalysisError
from route_analysis.main_window import MainWindow
from route_analysis.runtime_paths import resolve_data_dir
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import ConfigRepository, VehicleProfileRepository
from route_analysis.theme import APPLICATION_STYLESHEET


def main() -> int:
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv)
    app.setApplicationName("Suntae 路径通行分析")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Suntae")
    app.setStyle("Fusion")
    app.setStyleSheet(APPLICATION_STYLESHEET)

    data_dir = resolve_data_dir()
    config_repository = ConfigRepository(data_dir)
    profile_repository = VehicleProfileRepository(data_dir)
    try:
        config = config_repository.load()
        profiles = dict(profile_repository.load().values)
    except RouteAnalysisError as exc:
        QMessageBox.critical(None, "本地配置无法读取", str(exc))
        return 2

    if not config.first_run_complete:
        dialog = SettingsDialog(config, profiles, force_initial=True)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted or dialog.result_config is None:
            return 0
        try:
            config_repository.save(dialog.result_config)
            profile_repository.save(dialog.result_profiles)
        except RouteAnalysisError as exc:
            QMessageBox.critical(None, "首次设置无法保存", str(exc))
            return 2

    window = MainWindow(data_dir)
    window.show()
    return app.exec()
