import pytest
from pytestqt.qtbot import QtBot

from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    SOLVED_KEYS,
    Gear,
    Scenario,
    SolveMode,
)
from route_analysis.scenario_panel import ScenarioPanel

SOLVE_TIMEOUT_MS = 30_000
"""Forward solves in the heaviest variants run past a second on a busy machine."""


def _panel(qtbot: QtBot) -> ScenarioPanel:
    panel = ScenarioPanel()
    qtbot.addWidget(panel)
    panel.resize(1240, 720)
    panel.show()
    return panel


def _settled(qtbot: QtBot, panel: ScenarioPanel):
    """Wait on the signal, never by polling.

    ``qtbot.waitUntil`` spins on ``QTest.qWait``; the busy main thread then holds the GIL
    between switch intervals and starves the shapely work in the pool thread so badly
    that a sub-second solve does not finish inside the timeout.
    """

    with qtbot.waitSignal(panel.solved, timeout=SOLVE_TIMEOUT_MS) as caught:
        pass
    return caught.args[0]


def test_the_default_state_solves_a_road_without_any_data_source(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    result = _settled(qtbot, panel)
    assert result.inputs.mode is SolveMode.FORWARD
    assert result.inputs.scenario is Scenario.CORNER
    assert not result.infeasible
    assert result.status is ClearanceStatus.SAFE


def test_config_defaults_seed_the_vehicle_without_writing_back(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    dimensions = VehicleDimensions(width=1.4, center_front=1.2, center_rear=1.9)
    panel.set_vehicle_defaults(dimensions, 0.08)
    result = _settled(qtbot, panel)
    assert result.inputs.dimensions == dimensions
    assert result.inputs.threshold == pytest.approx(0.08)


def test_composite_scenarios_swap_the_gear_picker_for_a_fixed_pill(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    assert panel._gear_row.isVisible()
    assert not panel._fixed_gear_row.isVisible()
    panel._scenario_changed(Scenario.CROSSBACK)
    result = _settled(qtbot, panel)
    assert not panel._gear_row.isVisible()
    assert panel._fixed_gear_row.isVisible()
    assert result.inputs.effective_gear is Gear.DRIVE


def test_forward_mode_hides_the_road_inputs_it_is_going_to_solve(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    assert not panel._road_rows["wa"].isVisible()
    assert panel._road_note.isVisible()
    panel._mode_changed(SolveMode.CHECK)
    _settled(qtbot, panel)
    assert panel._road_rows["wa"].isVisible()
    assert not panel._road_note.isVisible()


def test_the_divider_stays_an_input_even_when_solving_forward(qtbot: QtBot) -> None:
    """The divider is site fabric, so it stays an input even when solving forward."""

    panel = _panel(qtbot)
    panel._scenario_changed(Scenario.UTURN)
    _settled(qtbot, panel)
    assert panel._road_rows["b"].isVisible()
    assert not panel._road_rows["w"].isVisible()


def test_a_solved_road_lands_in_the_inputs_so_check_mode_can_continue(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    result = _settled(qtbot, panel)
    for key in SOLVED_KEYS[Scenario.CORNER]:
        assert panel._road_spins[key].value() == pytest.approx(
            getattr(result.dims, key), abs=5e-3
        )


def test_only_the_last_change_in_a_burst_reaches_the_view(qtbot: QtBot) -> None:
    """Debounce plus a generation counter: a burst of clicks must drop stale results."""

    panel = _panel(qtbot)
    _settled(qtbot, panel)
    panel._scenario_changed(Scenario.CROSSBACK)
    panel._scenario_changed(Scenario.UTURN)
    panel._scenario_changed(Scenario.STUBBACK)
    result = _settled(qtbot, panel)
    assert result.inputs.scenario is Scenario.STUBBACK
    assert panel.result is not None
    assert panel.result.inputs.scenario is Scenario.STUBBACK


def test_a_radius_under_half_the_body_width_raises_the_notice(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    assert not panel._notice.isVisible()
    panel._vehicle_spins["radius"].setValue(0.60)
    assert panel._notice.isVisible()


def test_layer_toggles_redraw_without_solving_again(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    generation = panel._generation
    panel._layer_buttons["grid"].setChecked(False)
    panel._layer_buttons["envelope"].setChecked(False)
    assert panel._generation == generation
    assert not panel.plan._layers.grid
    assert not panel.plan._layers.envelope


def test_check_mode_reports_a_breach_without_popping_a_dialog(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    panel._mode_changed(SolveMode.CHECK)
    _settled(qtbot, panel)
    for key, value in (("wa", 3.0), ("wb", 3.0)):
        panel._road_spins[key].setValue(value)
    result = _settled(qtbot, panel)
    assert result.status is ClearanceStatus.OUTSIDE
    assert result.min_clearance < 0


def test_a_uturn_that_cannot_hold_the_radius_says_so_on_the_card(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    panel._mode_changed(SolveMode.CHECK)
    panel._scenario_changed(Scenario.UTURN)
    _settled(qtbot, panel)
    panel._road_spins["w"].setValue(1.80)
    panel._road_spins["b"].setValue(0.60)
    result = _settled(qtbot, panel)
    assert result.infeasible
    assert result.required_lane_width == pytest.approx(2.6)
