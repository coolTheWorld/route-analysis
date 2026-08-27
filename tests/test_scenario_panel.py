import time

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGroupBox, QScrollArea
from pytestqt.qtbot import QtBot

from route_analysis import scenario_panel, theme
from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    SOLVED_KEYS,
    Condition,
    Gear,
    Pins,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    offset_rows,
)
from route_analysis.scenario_graphics import (
    LEGEND_HEIGHT,
    LEGEND_ITEMS,
    LEGEND_ROW,
    BodySection,
    _transform,
    paint_scenario_plan,
)
from route_analysis.scenario_panel import (
    CENTRELINE_ROAD_NOTE,
    MIDDLE_ROW,
    OFFSET_ROW_KEYS,
    RESULT_WIDTH,
    ROAD_KEYS,
    RUN_CAPTION,
    SIDEBAR_WIDTH,
    ScenarioPanel,
)
from route_analysis.scenario_solver import solve_scenario, trace_maneuvers

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


def _pareto_panel(qtbot: QtBot, scenario: Scenario = Scenario.CORNER, **variant):
    """A shown panel already settled under the Pareto condition."""

    panel = _panel(qtbot)
    _settled(qtbot, panel)
    panel.select_variant(scenario=scenario, condition=Condition.PARETO, **variant)
    result = _settled(qtbot, panel)
    return panel, result


def test_the_default_state_solves_a_road_without_any_data_source(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    result = _settled(qtbot, panel)
    assert result.inputs.condition is Condition.CENTRELINE
    assert result.inputs.scenario is Scenario.CORNER
    assert not result.infeasible
    assert result.status is ClearanceStatus.SAFE
    assert result.solved_keys == SOLVED_KEYS[Scenario.CORNER]


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


def test_the_centreline_hides_the_dimensions_it_solves_and_pareto_lists_them(
    qtbot: QtBot,
) -> None:
    panel = _panel(qtbot)
    _settled(qtbot, panel)
    assert not panel._road_rows["wa"].isVisible()
    assert panel._road_note.isVisible()
    assert panel._road_note.text() == CENTRELINE_ROAD_NOTE
    panel._condition_changed(Condition.PARETO)
    _settled(qtbot, panel)
    assert panel._road_rows["wa"].isVisible()
    assert panel._road_rows["wb"].isVisible()
    assert panel._road_note.isVisible()
    assert panel._road_note.text() != CENTRELINE_ROAD_NOTE


def test_the_divider_stays_an_input_even_on_the_centreline(qtbot: QtBot) -> None:
    """The divider is site fabric, so it stays an input even when everything else is solved."""

    panel = _panel(qtbot)
    panel._scenario_changed(Scenario.UTURN)
    _settled(qtbot, panel)
    assert panel._road_rows["b"].isVisible()
    assert not panel._road_rows["w"].isVisible()


def test_the_centreline_shows_no_offset_rows_and_only_the_given_dimensions(
    qtbot: QtBot,
) -> None:
    panel = _panel(qtbot)
    panel._scenario_changed(Scenario.UTURN)
    _settled(qtbot, panel)
    shown = [key for key, row in panel._road_rows.items() if row.isVisible()]
    assert shown == ["b"]
    assert not any(holder.isVisible() for holder in panel._offset_holders.values())


def test_solved_values_are_written_back_into_the_inputs(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    result = _settled(qtbot, panel)
    for key in SOLVED_KEYS[Scenario.CORNER]:
        assert panel._road_spins[key].value() == pytest.approx(
            getattr(result.dims, key), abs=5e-3
        )


def test_pareto_lists_every_dimension_and_every_offset_of_the_layout(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.UTURN)
    shown = [key for key, row in panel._road_rows.items() if row.isVisible()]
    assert sorted(shown) == sorted(ROAD_KEYS[Scenario.UTURN])
    assert not panel._road_pins["b"].isVisible()
    assert panel._road_captions["b"].text().endswith("（给定）")
    assert panel._road_pins["w"].isVisible()
    assert panel._road_pins["d"].isVisible()
    listed = [row.key for row in offset_rows(Scenario.UTURN, bidirectional=False)]
    assert listed == ["e1", "e2", "yc"]
    for key in OFFSET_ROW_KEYS:
        assert panel._offset_holders[key].isVisible() == (key in listed), key


def test_editing_a_dimension_pins_it_and_the_solver_works_around_it(qtbot: QtBot) -> None:
    panel, before = _pareto_panel(qtbot)
    assert before.solved_keys == ("wa", "wb")
    panel._road_spins["wa"].setValue(3.5)
    assert "wa" in panel._pins.dims
    assert panel._road_pins["wa"].isChecked()
    assert not panel._road_pins["wb"].isChecked()
    result = _settled(qtbot, panel)
    assert "wa" in result.pins.dims
    assert result.dims.wa == pytest.approx(3.5)
    assert result.solved_keys == ("wb",)
    assert result.dims.wb < before.dims.wb


def test_releasing_a_pin_hands_the_dimension_back_and_refills_the_spin(qtbot: QtBot) -> None:
    panel, _before = _pareto_panel(qtbot)
    panel._road_spins["wa"].setValue(3.5)
    _settled(qtbot, panel)
    panel._road_pins["wa"].setChecked(False)
    assert "wa" not in panel._pins.dims
    result = _settled(qtbot, panel)
    assert "wa" in result.solved_keys
    assert result.dims.wa < 3.5
    assert panel._road_spins["wa"].value() == pytest.approx(result.dims.wa, abs=5e-3)


def test_editing_an_offset_pins_it_at_the_typed_value(qtbot: QtBot) -> None:
    panel, before = _pareto_panel(qtbot)
    assert before.offsets.ea != pytest.approx(0.2, abs=1e-3)
    panel._offset_spins["ea"].setValue(0.2)
    assert panel._pins.offsets == frozenset({"ea"})
    assert panel._offset_pins["ea"].isChecked()
    result = _settled(qtbot, panel)
    assert "ea" in result.pins.offsets
    assert result.offsets.ea == pytest.approx(0.2)
    assert not result.infeasible


def test_unpinned_offsets_show_what_the_solver_chose(qtbot: QtBot) -> None:
    panel, result = _pareto_panel(qtbot)
    assert result.pins == Pins()
    for row in offset_rows(Scenario.CORNER, bidirectional=False):
        assert row.key is not None
        assert panel._offset_spins[row.key].value() == pytest.approx(
            getattr(result.offsets, row.key), abs=5e-3
        ), row.key


def test_a_shared_leg_in_a_two_way_layout_is_listed_but_held_at_zero(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, bidirectional=True)
    shared = panel._offset_spins["eb"]
    assert panel._offset_holders["eb"].isVisible()
    assert not shared.isEnabled()
    assert shared.value() == pytest.approx(0.0)
    assert panel._offset_captions["eb"].text() == "支路偏移"
    assert panel._offset_shared["eb"].isVisible()
    assert not panel._offset_pins["eb"].isVisible()
    assert panel._offset_spins["ea"].isEnabled()
    assert panel._offset_pins["ea"].isVisible()


def test_a_two_way_uturn_lists_the_middle_aisle_it_shares(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.UTURN, bidirectional=True)
    assert panel._offset_holders[MIDDLE_ROW].isVisible()
    assert panel._offset_captions[MIDDLE_ROW].text() == "中巷偏移"
    assert panel._offset_shared[MIDDLE_ROW].isVisible()
    assert not panel._offset_spins[MIDDLE_ROW].isEnabled()
    assert not panel._offset_pins[MIDDLE_ROW].isVisible()
    assert panel._offset_holders["eo"].isVisible()
    assert panel._offset_holders["yc"].isVisible()
    assert not panel._offset_holders["e1"].isVisible()
    assert not panel._offset_holders["e2"].isVisible()


@pytest.mark.parametrize(
    "switch",
    [
        lambda panel: panel._scenario_changed(Scenario.CROSSBACK),
        lambda panel: panel._direction_changed(True),
    ],
    ids=["scenario", "direction"],
)
def test_changing_the_layout_releases_every_pin(switch, qtbot: QtBot) -> None:
    panel, _before = _pareto_panel(qtbot)
    panel._road_spins["wa"].setValue(3.5)
    panel._offset_spins["ea"].setValue(0.2)
    _settled(qtbot, panel)
    assert panel._pins != Pins()
    switch(panel)
    result = _settled(qtbot, panel)
    assert panel._pins == Pins()
    assert result.pins == Pins()
    assert not any(pin.isChecked() for pin in panel._road_pins.values())
    assert not any(pin.isChecked() for pin in panel._offset_pins.values())


def test_changing_the_vehicle_keeps_the_pins(qtbot: QtBot) -> None:
    panel, _before = _pareto_panel(qtbot)
    panel._road_spins["wa"].setValue(3.5)
    _settled(qtbot, panel)
    panel._vehicle_spins["radius"].setValue(1.40)
    result = _settled(qtbot, panel)
    assert result.inputs.radius == pytest.approx(1.40)
    assert result.pins.dims == frozenset({"wa"})
    assert result.dims.wa == pytest.approx(3.5)
    assert panel._road_pins["wa"].isChecked()


def test_pinning_every_dimension_turns_the_solve_into_a_verdict(qtbot: QtBot) -> None:
    panel, _before = _pareto_panel(qtbot)
    panel._road_spins["wa"].setValue(3.0)
    panel._road_spins["wb"].setValue(3.0)
    result = _settled(qtbot, panel)
    assert result.pins.dims == frozenset(SOLVED_KEYS[Scenario.CORNER])
    assert result.solved_keys == ()
    assert not result.solved
    assert not result.infeasible
    assert panel._status_card._title.text() == "判定"
    assert panel._dimension_card._title.text() == "给定道路尺寸"


def test_a_pinned_road_that_breaches_says_so_without_popping_a_dialog(qtbot: QtBot) -> None:
    panel, _before = _pareto_panel(qtbot)
    panel._road_spins["wa"].setValue(2.0)
    panel._road_spins["wb"].setValue(2.0)
    result = _settled(qtbot, panel)
    assert result.solved_keys == ()
    assert result.status is ClearanceStatus.OUTSIDE
    assert result.min_clearance < 0


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


def test_a_uturn_that_cannot_hold_the_radius_says_so_on_the_card(qtbot: QtBot) -> None:
    """With every dimension pinned the layout is checked, and the shortfall is named.

    Pinning only ``w`` leaves ``d`` to solve; that search fails first and reports a plain
    "no feasible road" without the radius arithmetic, so the road is pinned outright.
    """

    panel, _before = _pareto_panel(qtbot, Scenario.UTURN)
    panel._vehicle_spins["radius"].setValue(1.60)
    panel._road_spins["w"].setValue(1.80)
    panel._road_spins["d"].setValue(5.00)
    panel._road_spins["b"].setValue(0.60)
    assert panel._pins.dims == frozenset({"w", "d"})
    result = _settled(qtbot, panel)
    assert result.infeasible
    assert result.solved_keys == ()
    # Leaning the free aisle offsets outwards widens the circle, so less than 2R - b.
    assert result.required_lane_width is not None
    assert 1.80 < result.required_lane_width < 2.6
    assert panel._status_card._title.text() == "求解状态"
    assert f"{result.required_lane_width:.2f}" in panel._status_card._note.text()


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


def test_the_plan_header_controls_fit_without_eliding(qtbot: QtBot) -> None:
    """The controls must fit the plan column, or QToolButton quietly elides them to "…".

    Measured at full-width CJK metrics rather than through QFontMetrics: a machine with no
    CJK font reports box glyphs and says everything fits when it does not. Three body
    section buttons used to sit here too, which pushed the row past the column and elided
    the captions and the title together.
    """

    panel = _panel(qtbot)
    _settled(qtbot, panel)
    captions = [panel._run_button.text()] + [
        button.text() for button in panel._layer_buttons.values()
    ]
    need = sum(_cjk_advance(text, 11.0) + 18.0 for text in captions) + 8.0 * (
        len(captions) + 1
    )
    column = panel.width() - SIDEBAR_WIDTH - RESULT_WIDTH - 24
    assert need <= column, (need, column, captions)


def test_the_body_section_picker_sits_with_the_dimensions_it_splits_on(
    qtbot: QtBot,
) -> None:
    """It belongs beside 中心前距 / 中心后距, not in the plan header it used to overflow."""

    panel = _panel(qtbot)
    _settled(qtbot, panel)
    owner = panel._section_segment.parent()
    while owner is not None and not isinstance(owner, QGroupBox):
        owner = owner.parent()
    assert isinstance(owner, QGroupBox)
    assert owner.title() == "车辆参数"


def test_every_dot_on_the_plan_has_its_own_colour() -> None:
    """The markers carry no caption, so colour is the only thing telling them apart.

    Start, end and tightest-clearance are all plain dots. End used to share the danger red
    with the clearance point, which was survivable only while each dot was labelled on the
    plan; the captions covered the dots they named and came off.
    """

    dots = [(name, colour) for name, colour, shape in LEGEND_ITEMS if shape == "dot"]
    assert len(dots) >= 3
    colours = [colour for _name, colour in dots]
    assert len(set(colours)) == len(colours), dots


@pytest.mark.parametrize("condition", list(Condition))
@pytest.mark.parametrize("scenario", list(Scenario))
def test_the_endpoint_dots_are_never_painted_over(scenario, condition, qtbot: QtBot) -> None:
    """Render and read the actual pixel under each dot.

    They carry no caption now, so a dot that something else covers is simply gone. The
    dimension marks used to land on top: with an offset moving the stub start under the
    出弯主路深度 label, its halo erased the dot entirely. Colours survive a machine with no
    CJK font, unlike anything that measures text.
    """

    inputs = ScenarioInputs(
        scenario=scenario, condition=condition,
        dimensions=VehicleDimensions(width=1.23, center_front=1.545, center_rear=2.223),
        radius=1.20, threshold=0.15,
    )
    result = solve_scenario(inputs, RoadDimensions())
    image = QImage(900, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    paint_scenario_plan(painter, QRectF(0, 0, 900, 600), result)
    painter.end()

    traces = trace_maneuvers(result.layout, inputs.dimensions)
    plan = QRectF(8, 8, 900 - 16, 600 - LEGEND_HEIGHT - 20)
    transform = _transform(plan, result.layout.view_bounds)
    for trace in traces:
        for index, colour in ((0, theme.SUCCESS_BAR), (len(trace.x) - 1, theme.ENDPOINT_END)):
            point = transform.point(float(trace.x[index]), float(trace.y[index]))
            found = any(
                QColor(image.pixel(int(point.x()) + dx, int(point.y()) + dy)).name().lower()
                == colour.lower()
                for dx in range(-2, 3)
                for dy in range(-2, 3)
            )
            assert found, (scenario, condition, trace.label, colour)


def test_the_gear_and_the_condition_also_release_every_pin(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    panel._road_spins["wa"].setValue(3.5)
    _settled(qtbot, panel)
    assert "wa" in panel._pins.dims
    panel._gear_changed(Gear.REVERSE)
    _settled(qtbot, panel)
    assert panel._pins == Pins()
    assert not panel._road_pins["wa"].isChecked()
    panel._road_spins["wa"].setValue(3.5)
    _settled(qtbot, panel)
    assert "wa" in panel._pins.dims
    panel._condition_changed(Condition.CENTRELINE)
    _settled(qtbot, panel)
    assert panel._pins == Pins()


def test_clicking_the_selected_choice_again_keeps_the_pins(qtbot: QtBot) -> None:
    """Re-clicking what is already selected is not a switch, so nothing is released."""

    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    panel._road_spins["wa"].setValue(3.5)
    _settled(qtbot, panel)
    generation = panel._generation
    panel._condition_changed(Condition.PARETO)
    panel._scenario_changed(Scenario.CORNER)
    panel._direction_changed(False)
    panel._gear_changed(Gear.DRIVE)
    assert "wa" in panel._pins.dims
    assert panel._generation == generation


def test_releasing_an_offset_pin_hands_it_back_and_refills_the_spin(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    panel._offset_spins["ea"].setValue(0.2)
    _settled(qtbot, panel)
    assert "ea" in panel._pins.offsets
    panel._offset_pins["ea"].setChecked(False)
    result = _settled(qtbot, panel)
    assert "ea" not in result.pins.offsets
    assert panel._offset_spins["ea"].value() == pytest.approx(result.offsets.ea, abs=5e-3)
    assert panel._offset_spins["ea"].value() != pytest.approx(0.2, abs=1e-3)


def test_the_pin_toggle_freezes_the_value_on_screen(qtbot: QtBot) -> None:
    """What the toggle pins is the number in the box, not whatever the model last held."""

    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    spin = panel._road_spins["wa"]
    spin.blockSignals(True)
    spin.setValue(3.30)
    spin.blockSignals(False)
    assert panel._road.wa != pytest.approx(3.30)
    panel._road_pins["wa"].setChecked(True)
    result = _settled(qtbot, panel)
    assert "wa" in result.pins.dims
    assert result.dims.wa == pytest.approx(3.30)


def test_typing_a_value_and_clicking_pin_at_once_pins_the_typed_value(qtbot: QtBot) -> None:
    """Keyboard tracking is off, so the typed text is uncommitted when the pin is clicked.

    The click must not take focus (that would commit the text, auto-pin the row and let
    the same click's toggle release it again); the toggle commits the text itself.
    """

    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    spin = panel._road_spins["wa"]
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "3.5")
    assert spin.value() != pytest.approx(3.5)
    QTest.mouseClick(panel._road_pins["wa"], Qt.MouseButton.LeftButton)
    result = _settled(qtbot, panel)
    assert "wa" in result.pins.dims
    assert result.dims.wa == pytest.approx(3.5)
    assert panel._road_pins["wa"].isChecked()


def test_a_focused_but_untouched_spin_still_takes_the_refill(qtbot: QtBot) -> None:
    """Focus alone is not typing: the cursor resting in a box must not freeze a stale value."""

    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    panel._road_spins["wa"].setValue(3.5)
    panel._road_spins["wb"].setFocus()
    result = _settled(qtbot, panel)
    assert panel._road_spins["wb"].value() == pytest.approx(result.dims.wb, abs=5e-3)


def test_a_result_in_flight_cannot_overwrite_a_value_pinned_meanwhile(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every input change moves the generation, so a stale solve is dropped, not applied.

    The solve is slowed down so the edit is certain to land while it is still running;
    without the generation bump the stale result would arrive first, refill ``wa`` with
    the solver's value and reach the ``solved`` signal with no pin at all.
    """

    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    real = scenario_panel.solve_scenario

    def slow(*args, **kwargs):
        time.sleep(0.6)
        return real(*args, **kwargs)

    monkeypatch.setattr(scenario_panel, "solve_scenario", slow)
    panel._vehicle_spins["radius"].setValue(1.40)
    panel._timer.stop()
    panel._start_solve()
    panel._road_spins["wa"].setValue(3.5)
    result = _settled(qtbot, panel)
    assert "wa" in result.pins.dims
    assert result.dims.wa == pytest.approx(3.5)
    assert panel._road_spins["wa"].value() == pytest.approx(3.5)


def test_the_wheel_only_turns_a_focused_spin(qtbot: QtBot) -> None:
    """Scrolling the sidebar must not edit -- and so pin -- whatever box the pointer crosses."""

    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    spin = panel._road_spins["wa"]
    panel._road_spins["wb"].setFocus()
    before = spin.value()
    event = QWheelEvent(
        QPointF(5, 5), spin.mapToGlobal(QPoint(5, 5)), QPoint(0, 120), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(spin, event)
    assert spin.value() == pytest.approx(before)
    assert "wa" not in panel._pins.dims
    spin.setFocus()
    QApplication.sendEvent(spin, event)
    assert spin.value() != pytest.approx(before)
    assert "wa" in panel._pins.dims


def test_releasing_a_pin_drops_text_that_was_never_committed(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.CORNER)
    spin = panel._road_spins["wa"]
    spin.setValue(3.5)
    _settled(qtbot, panel)
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "4.2")
    panel._road_pins["wa"].setChecked(False)
    assert not spin.lineEdit().isModified()
    assert "4.2" not in spin.text()
    result = _settled(qtbot, panel)
    assert "wa" not in result.pins.dims


def test_shared_rows_carry_a_tag_instead_of_a_pin_and_keep_short_captions(
    qtbot: QtBot,
) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.CROSSBACK, bidirectional=True)
    assert panel._offset_captions["ev"].text() == "倒车道偏移"
    assert panel._offset_shared["ev"].isVisible()
    assert not panel._offset_pins["ev"].isVisible()
    assert panel._offset_pins["eh"].isVisible()
    assert not panel._offset_shared["eh"].isVisible()
    for caption in panel._offset_captions.values():
        assert len(caption.text()) <= 8


def test_the_sidebar_content_fits_its_viewport_under_pareto(qtbot: QtBot) -> None:
    panel, _result = _pareto_panel(qtbot, Scenario.UTURN, bidirectional=True)
    side = panel.findChild(QScrollArea)
    assert side is not None
    assert side.widget().minimumSizeHint().width() <= side.viewport().width()


def test_the_export_button_arms_after_a_result_and_asks_the_window_to_export(
    qtbot: QtBot,
) -> None:
    panel = ScenarioPanel()
    qtbot.addWidget(panel)
    assert not panel._export_button.isEnabled()
    panel.resize(1240, 720)
    panel.show()
    _settled(qtbot, panel)
    assert panel._export_button.isEnabled()
    with qtbot.waitSignal(panel.export_pdf_requested, timeout=1000):
        panel._export_button.click()
