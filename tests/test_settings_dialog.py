from pytestqt.qtbot import QtBot

from route_analysis.models import VehicleDimensions
from route_analysis.settings_dialog import SettingsDialog
from route_analysis.storage import AppConfig


def test_settings_dialog_collects_required_values_and_vin_profiles(qtbot: QtBot) -> None:
    dialog = SettingsDialog(AppConfig(), {}, force_initial=True)
    qtbot.addWidget(dialog)
    dialog.api_root_edit.setText("http://example.test/admin-api")
    dialog.username_edit.setText("operator")
    dialog.password_edit.setText("plain-secret")
    dialog.width_spin.setValue(1.2)
    dialog.front_spin.setValue(0.8)
    dialog.rear_spin.setValue(1.4)
    dialog.lane_width_spin.setValue(2.5)
    dialog.map_direction_spin.setValue(1.57)
    dialog.add_profile("VIN-1", VehicleDimensions(1.3, 0.9, 1.5))

    dialog.accept()

    assert dialog.result_config is not None
    assert dialog.result_config.tenant == "suntae"
    assert dialog.result_config.default_vehicle == VehicleDimensions(1.2, 0.8, 1.4)
    assert dialog.result_config.default_lane_width == 2.5
    assert dialog.result_config.map_direction == 1.57
    assert dialog.result_profiles == {"VIN-1": VehicleDimensions(1.3, 0.9, 1.5)}
    assert dialog.password_edit.echoMode() == dialog.password_edit.EchoMode.Normal
