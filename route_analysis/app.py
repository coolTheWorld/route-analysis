"""Application entry point and first-run gate."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication, QMessageBox

from route_analysis import __version__
from route_analysis.errors import RouteAnalysisError
from route_analysis.main_window import MainWindow
from route_analysis.models import Lane, Point2D, PosePoint, VehicleDimensions
from route_analysis.runtime_paths import resolve_data_dir
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import ConfigRepository, VehicleProfileRepository
from route_analysis.theme import APPLICATION_STYLESHEET


def _run_smoke_test(app: QApplication) -> int:
    """Exercise bundled Qt and GEOS code without reading user configuration."""

    from route_analysis.canvas import RouteCanvas
    from route_analysis.geometry import build_lane_area
    from route_analysis.storage import LaneLayout

    lane = Lane.create("smoke", "Smoke", 2, [Point2D(0, 0), Point2D(2, 0)])
    if not build_lane_area(lane).is_valid:
        return 3
    canvas = RouteCanvas()
    canvas.load_layout(LaneLayout("0000000000000000", "0", [lane]))
    canvas.set_paths(
        (PosePoint(0, 0, 0),),
        (),
        VehicleDimensions(1, 1, 1),
    )
    app.processEvents()
    canvas.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(arguments)
    app.setApplicationName("Suntae 路径通行分析")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Suntae")
    app.setStyle("Fusion")
    app.setStyleSheet(APPLICATION_STYLESHEET)

    if "--smoke-test" in arguments:
        return _run_smoke_test(app)

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
