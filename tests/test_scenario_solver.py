import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from route_analysis import scenario_geometry
from route_analysis.clearance_geometry import corner_radii
from route_analysis.models import ClearanceStatus, VehicleDimensions
from route_analysis.scenario_geometry import (
    SOLVED_KEYS,
    Gear,
    Offsets,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    SolveMode,
    build_layout,
)
from route_analysis.scenario_solver import (
    COARSE,
    FINE,
    Steps,
    evaluate,
    evaluate_layout,
    offset_specs,
    optimise_offsets,
    sample_poses,
    solve_forward,
    solve_scenario,
)

DIMENSIONS = VehicleDimensions(width=1.23, center_front=1.545, center_rear=2.223)
FIXTURE = Path(__file__).parent / "fixtures" / "scenario_clearance.json"

DIMENSION_KEYS = {
    "wA": "wa", "wB": "wb", "wV": "wv", "wH": "wh",
    "LS": "ls", "w": "w", "b": "b", "D": "d",
}
OFFSET_KEYS = {
    "eA": "ea", "eB": "eb", "eV": "ev", "eH": "eh", "a": "a",
    "so": "so", "e1": "e1", "e2": "e2", "eo": "eo", "yc": "yc",
}
PROTOTYPE_END_WALL = 0.15
"""What the prototype reports when its artificial end wall binds: a fixed 0.15 m trim."""


def _inputs(**overrides) -> ScenarioInputs:
    base = {"dimensions": DIMENSIONS, "radius": 1.60, "threshold": 0.05}
    return ScenarioInputs(**(base | overrides))


def _fixture_cases():
    data = json.loads(FIXTURE.read_text())
    return data["vehicle"], data["cases"]


def test_geometry_layer_matches_the_prototype_case_for_case(monkeypatch):
    """Case-for-case cross-check against the prototype: region, path, sampling, mirror, gear.

    With the end-wall padding at zero the region matches the prototype exactly, so every
    case has to agree to the last digit. The padding itself is covered separately by
    ``test_padded_end_walls_only_ever_loosen_the_result``.
    """

    monkeypatch.setattr(scenario_geometry, "CAP_PAD", 0.0)
    vehicle, cases = _fixture_cases()
    dimensions = VehicleDimensions(
        width=vehicle["W"], center_front=vehicle["Lf"], center_rear=vehicle["Lr"]
    )
    compared = 0
    for case in cases:
        inputs = _inputs(
            scenario=Scenario(case["type"]),
            mode=SolveMode.CHECK,
            bidirectional=case["bidir"],
            gear=Gear(case["gear"]),
            dimensions=dimensions,
            radius=vehicle["R"],
        )
        dims = RoadDimensions(
            **{DIMENSION_KEYS[key]: value for key, value in case["dims"].items()}
        )
        offsets = Offsets(
            **{OFFSET_KEYS[key]: value for key, value in case["off"].items()}
        )
        steps = Steps(case["arc_step"], case["line_step"])
        actual = evaluate(inputs, dims, offsets, steps, detail=True).clearance
        assert actual == pytest.approx(case["clearance"], abs=1e-6), case
        compared += 1
    assert compared > 200, "对拍用例太少，夹具可能没重新生成"


def test_padded_end_walls_only_ever_loosen_the_result():
    """Padding the end walls removes a constraint, so clearance can only grow."""

    vehicle, cases = _fixture_cases()
    dimensions = VehicleDimensions(
        width=vehicle["W"], center_front=vehicle["Lf"], center_rear=vehicle["Lr"]
    )
    loosened = 0
    for case in cases:
        inputs = _inputs(
            scenario=Scenario(case["type"]),
            mode=SolveMode.CHECK,
            bidirectional=case["bidir"],
            gear=Gear(case["gear"]),
            dimensions=dimensions,
            radius=vehicle["R"],
        )
        dims = RoadDimensions(
            **{DIMENSION_KEYS[key]: value for key, value in case["dims"].items()}
        )
        offsets = Offsets(
            **{OFFSET_KEYS[key]: value for key, value in case["off"].items()}
        )
        actual = evaluate(
            inputs, dims, offsets, Steps(case["arc_step"], case["line_step"]), detail=True
        ).clearance
        assert actual >= case["clearance"] - 1e-6, case
        if actual > case["clearance"] + 1e-6:
            loosened += 1
    assert loosened, "没有任何用例被端墙顶住，夹具已经失去回归价值"


def test_the_prototype_end_wall_capped_every_roomy_case():
    """Record what was fixed: give the prototype room and its clearance pins to the trim."""

    _, cases = _fixture_cases()
    capped = [
        case
        for case in cases
        if math.isclose(case["clearance"], PROTOTYPE_END_WALL, abs_tol=1e-9)
    ]
    assert len(capped) > 50


def test_four_corner_radii_come_from_the_closed_form():
    inputs = _inputs(scenario=Scenario.CORNER, mode=SolveMode.CHECK)
    result = solve_scenario(inputs, RoadDimensions())
    assert result.corner_radii == corner_radii(DIMENSIONS, inputs.radius)


@pytest.mark.parametrize("scenario", list(Scenario))
def test_the_two_mirrored_maneuvers_are_equally_tight(scenario):
    """A two-way layout is symmetric, so both mirrored maneuvers are equally tight."""

    inputs = _inputs(scenario=scenario, bidirectional=True, mode=SolveMode.CHECK)
    layout = build_layout(inputs, RoadDimensions(), Offsets())
    values = [
        evaluate_layout(
            replace(layout, maneuvers=(maneuver,)), DIMENSIONS, FINE
        ).clearance
        for maneuver in layout.maneuvers
    ]
    assert values[0] == pytest.approx(values[1], abs=1e-9)


def test_gear_swaps_which_leg_needs_the_extra_width():
    """D eats the rear outer corner and R the front outer, swapping what each leg needs."""

    drive = solve_scenario(
        _inputs(scenario=Scenario.CORNER, gear=Gear.DRIVE), RoadDimensions()
    )
    reverse = solve_scenario(
        _inputs(scenario=Scenario.CORNER, gear=Gear.REVERSE), RoadDimensions()
    )
    assert drive.dims.wa == pytest.approx(reverse.dims.wb, abs=2e-3)
    assert drive.dims.wb == pytest.approx(reverse.dims.wa, abs=2e-3)


@pytest.mark.parametrize(
    ("scenario", "bidirectional", "extreme"),
    [
        (Scenario.CORNER, False, False),
        (Scenario.CORNER, True, True),
        (Scenario.CROSSBACK, False, False),
        (Scenario.STUBBACK, False, False),
        (Scenario.UTURN, False, False),
        (Scenario.UTURN, True, True),
    ],
)
def test_solved_dimensions_fed_back_in_still_clear_the_threshold(
    scenario, bidirectional, extreme
):
    """The headline check: feed a solved road back in and it must still clear the threshold."""

    inputs = _inputs(
        scenario=scenario, bidirectional=bidirectional, extreme=extreme,
        mode=SolveMode.FORWARD,
    )
    solved = solve_scenario(inputs, RoadDimensions())
    assert not solved.infeasible
    checked = solve_scenario(
        _inputs(
            scenario=scenario, bidirectional=bidirectional, extreme=extreme,
            mode=SolveMode.CHECK,
        ),
        solved.dims,
    )
    assert checked.min_clearance >= inputs.threshold - 1e-6
    assert checked.status is ClearanceStatus.SAFE


@pytest.mark.parametrize("scenario", list(Scenario))
def test_a_bigger_threshold_never_asks_for_a_smaller_road(scenario):
    previous = None
    for threshold in (0.05, 0.15, 0.30):
        result = solve_scenario(
            _inputs(scenario=scenario, threshold=threshold, mode=SolveMode.FORWARD),
            RoadDimensions(),
        )
        assert not result.infeasible, (scenario, threshold)
        current = [getattr(result.dims, key) for key in SOLVED_KEYS[scenario]]
        if previous is not None:
            assert sum(current) >= sum(previous) - 1e-6
        previous = current


def test_a_generous_threshold_is_still_solvable():
    """Regression: with end walls in the measurement, any threshold above the path trim

    made every scenario permanently infeasible.
    """

    result = solve_scenario(
        _inputs(scenario=Scenario.CORNER, threshold=0.30, mode=SolveMode.FORWARD),
        RoadDimensions(),
    )
    assert not result.infeasible
    assert result.min_clearance == pytest.approx(0.30, abs=5e-3)


def test_widening_the_road_never_tightens_the_clearance():
    previous = None
    for width in (2.6, 3.0, 3.6, 4.4):
        result = solve_scenario(
            _inputs(scenario=Scenario.CORNER, mode=SolveMode.CHECK),
            RoadDimensions(wa=width, wb=width),
        )
        if previous is not None:
            assert result.min_clearance >= previous - 1e-9
        previous = result.min_clearance


def test_bidirectional_uturn_can_never_beat_the_one_way_layout():
    """Two-way shares the middle aisle, so symmetry pins its offset to zero and the

    feasible set is a strict subset of the one-way case.
    """

    for extreme in (False, True):
        values = [
            solve_scenario(
                _inputs(
                    scenario=Scenario.UTURN, bidirectional=bidirectional,
                    extreme=extreme, mode=SolveMode.CHECK,
                ),
                RoadDimensions(),
            ).min_clearance
            for bidirectional in (False, True)
        ]
        assert values[1] <= values[0] + 1e-9
    assert values[1] < values[0]


def test_uturn_that_cannot_hold_the_radius_says_how_wide_it_must_be():
    result = solve_scenario(
        _inputs(scenario=Scenario.UTURN, mode=SolveMode.CHECK),
        RoadDimensions(w=1.8, b=0.6),
    )
    assert result.infeasible
    assert result.radius_shortfall == pytest.approx(0.4)
    assert result.required_lane_width == pytest.approx(2.6)


def test_an_impossible_threshold_reports_the_ceiling_it_could_reach():
    result = solve_scenario(
        _inputs(scenario=Scenario.CORNER, threshold=8.0, mode=SolveMode.FORWARD),
        RoadDimensions(),
    )
    assert result.infeasible
    assert result.threshold_ceiling is not None
    assert 0.0 <= result.threshold_ceiling < 8.0


def test_status_follows_the_same_rule_as_the_overrun_analysis():
    tight = solve_scenario(
        _inputs(scenario=Scenario.CORNER, mode=SolveMode.CHECK),
        RoadDimensions(wa=3.0, wb=3.0),
    )
    assert tight.min_clearance < 0
    assert tight.status is ClearanceStatus.OUTSIDE
    marginal = solve_scenario(
        _inputs(scenario=Scenario.CORNER, mode=SolveMode.CHECK, threshold=0.6),
        RoadDimensions(wa=4.2, wb=4.2),
    )
    assert 0 <= marginal.min_clearance < 0.6
    assert marginal.status is ClearanceStatus.WARNING


def test_the_offsets_the_optimiser_returns_stay_inside_their_own_bounds():
    inputs = _inputs(scenario=Scenario.CORNER, extreme=True, mode=SolveMode.CHECK)
    dims = RoadDimensions()
    offsets, _ = optimise_offsets(inputs, dims, COARSE, cheap=True)
    for spec in offset_specs(inputs, dims):
        assert spec.low - 1e-9 <= getattr(offsets, spec.key) <= spec.high + 1e-9


@pytest.mark.parametrize(
    ("scenario", "dims"),
    [
        (Scenario.CORNER, RoadDimensions(wa=3.6, wb=3.6)),
        (Scenario.CORNER, RoadDimensions(wa=3.0, wb=3.0)),
        (Scenario.STUBBACK, RoadDimensions()),
        (Scenario.CROSSBACK, RoadDimensions()),
        (Scenario.UTURN, RoadDimensions()),
    ],
)
def test_feasible_bands_always_contain_the_offset_they_report(scenario, dims):
    """The reported offset has to sit inside its own band or two rows contradict."""

    result = solve_scenario(
        _inputs(scenario=scenario, extreme=True, mode=SolveMode.CHECK), dims
    )
    assert result.bands
    for band in result.bands:
        assert not band.empty, band
        chosen = getattr(result.offsets, band.key)
        assert band.low <= chosen <= band.high, (band, chosen)


def test_centred_comparison_matches_the_centreline_variant():
    """The comparison row must land on the same number the centreline variant reports."""

    extreme = solve_scenario(
        _inputs(scenario=Scenario.UTURN, extreme=True, mode=SolveMode.CHECK),
        RoadDimensions(),
    )
    centred = solve_scenario(
        _inputs(scenario=Scenario.UTURN, extreme=False, mode=SolveMode.CHECK),
        RoadDimensions(),
    )
    assert extreme.centred_clearance == pytest.approx(centred.min_clearance, abs=1e-9)


def test_pose_sampling_puts_the_body_where_the_gear_says():
    inputs = _inputs(scenario=Scenario.CORNER, gear=Gear.DRIVE)
    layout = build_layout(inputs, RoadDimensions(), Offsets())
    samples = sample_poses(layout.maneuvers[0].primitives, DIMENSIONS, FINE)
    assert len(samples) > 100
    lead = samples.corners[0][0]
    assert lead[0] == pytest.approx(samples.x[0] + DIMENSIONS.center_front, abs=1e-9)


def test_forward_solution_reports_the_ceiling_even_when_it_succeeds():
    solution = solve_forward(
        _inputs(scenario=Scenario.CORNER, mode=SolveMode.FORWARD), RoadDimensions()
    )
    assert solution.dims is not None
    assert solution.ceiling_clearance > 0.05
