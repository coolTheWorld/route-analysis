import pytest
from pytestqt.qtbot import QtBot

from route_analysis import theme
from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    SOLVED_KEYS,
    Gear,
    Scenario,
    SolveMode,
)
from route_analysis.scenario_graphics import (
    LEGEND_HEIGHT,
    LEGEND_ITEMS,
    LEGEND_ROW,
    BodySection,
)
from route_analysis.scenario_panel import RUN_CAPTION, ScenarioPanel

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
    panel._vehicle_spins["radius"].setValue(1.60)
    panel._road_spins["w"].setValue(1.80)
    panel._road_spins["b"].setValue(0.60)
    result = _settled(qtbot, panel)
    assert result.infeasible
    assert result.required_lane_width == pytest.approx(2.6)


def test_the_envelope_can_show_one_end_of_the_body_at_a_time(qtbot: QtBot) -> None:
    """Sectioning is a drawing choice, so it must not cost a solve."""

    panel = _panel(qtbot)
    _settled(qtbot, panel)
    assert panel.plan._layers.section is BodySection.WHOLE
    generation = panel._generation
    for section in (BodySection.FRONT, BodySection.REAR, BodySection.WHOLE):
        panel._section_segment._buttons[section].click()
        assert panel.plan._layers.section is section
    assert panel._generation == generation


def test_the_two_body_sections_are_named_in_the_legend(qtbot: QtBot) -> None:
    """Fixed colours are only readable if the legend says which half each one is."""

    captions = [item[0] for item in LEGEND_ITEMS]
    colours = {item[0]: item[1] for item in LEGEND_ITEMS}
    front = next(name for name in captions if "前段" in name)
    rear = next(name for name in captions if "后段" in name)
    assert "中心前距" in front and "中心后距" in rear
    assert colours[front] == theme.SECTION_FRONT
    assert colours[rear] == theme.SECTION_REAR
    assert colours[front] != colours[rear]


def test_the_run_through_walks_the_playhead_and_stops_at_the_end(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    assert panel.plan._playhead is None
    panel._run_button.click()
    assert panel._run_timer.isActive()
    assert panel.plan._playhead == pytest.approx(0.0)
    seen = []
    for _ in range(400):
        if not panel._run_timer.isActive():
            break
        panel._advance_run()
        if panel.plan._playhead is not None:
            seen.append(panel.plan._playhead)
    assert not panel._run_timer.isActive(), "跑完必须自己停下"
    assert panel.plan._playhead == pytest.approx(1.0)
    assert seen == sorted(seen), "播放位置只能单向前进"
    assert panel._run_button.text() == RUN_CAPTION


def test_starting_a_new_solve_abandons_a_run_in_progress(qtbot: QtBot) -> None:
    """The path is rebuilt underneath it, so a part-way run-through no longer means anything."""

    panel = _panel(qtbot)
    _settled(qtbot, panel)
    panel._run_button.click()
    assert panel._run_timer.isActive()
    panel._scenario_changed(Scenario.UTURN)
    _settled(qtbot, panel)
    assert not panel._run_timer.isActive()
    assert panel.plan._playhead is None
    assert panel._run_button.text() == RUN_CAPTION


def _cjk_advance(message: str, size: float = 10.5) -> float:
    """Width at full-width CJK metrics, so the check does not depend on installed fonts.

    Measuring through QFontMetrics on a machine with no CJK font gives narrow box glyphs
    and reports the legend fitting when it does not.
    """

    return sum(size if ord(ch) > 0x2E80 else size * 0.55 for ch in message)


def test_the_legend_never_silently_drops_an_entry() -> None:
    """It wraps; it used to walk one row and stop, losing whatever came last.

    Eleven entries at full-width metrics need about 940 px against a plan column nearer
    640 px, and the two section colours are meaningless without the words beside them.
    """

    rows = int(LEGEND_HEIGHT // LEGEND_ROW)
    assert rows >= 2
    for width in (600.0, 640.0, 900.0):
        x, row, placed = 0.0, 0, 0
        for message, _colour, _shape in LEGEND_ITEMS:
            span = 16.0 + _cjk_advance(message) + 16.0
            if x > 0.0 and x + span > width:
                x, row = 0.0, row + 1
                if row >= rows:
                    break
            placed += 1
            x += span
        assert placed == len(LEGEND_ITEMS), (width, placed, len(LEGEND_ITEMS))
