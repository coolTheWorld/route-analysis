import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
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

SUPERSEDED_BY_REQUIREMENT = {Scenario.STUBBACK}
"""Scenarios whose maneuver no longer matches the prototype the fixture was taken from.

The prototype drove stubback as a U-turn borrowed through the branch -- approach, reverse
in, pull out the far side -- which the design handoff itself flagged as unconfirmed. The
requirement owner has since settled it: the truck starts inside the branch nose-south,
reverses through one quarter turn onto the east side of the trunk, and drives away west.
Different path, so the prototype's readings are no longer the same question and comparing
against them would only pin the old shape back in place. The remaining scenarios still
cross-check case for case.
"""


def _comparable(cases):
    return [case for case in cases if Scenario(case["type"]) not in SUPERSEDED_BY_REQUIREMENT]


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
    cases = _comparable(cases)
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
    assert compared > 170, "对拍用例太少，夹具可能没重新生成"


def test_padded_end_walls_only_ever_loosen_the_result():
    """Padding the end walls removes a constraint, so clearance can only grow."""

    vehicle, cases = _fixture_cases()
    dimensions = VehicleDimensions(
        width=vehicle["W"], center_front=vehicle["Lf"], center_rear=vehicle["Lr"]
    )
    cases = _comparable(cases)
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


def _search_ceilings(inputs: ScenarioInputs, given: RoadDimensions) -> dict[str, float]:
    """The upper end of each solved dimension's bracket, mirroring ``solve_forward``."""

    vehicle = inputs.dimensions
    width_high = (
        vehicle.width
        + 2 * inputs.threshold
        + 2 * max(math.hypot(inputs.radius + vehicle.width / 2, vehicle.center_rear)
                  - inputs.radius, inputs.radius)
        + 2.4
    )
    lane_seed = max(vehicle.width + 2 * inputs.threshold, 2 * inputs.radius - given.b) + 1
    return {
        "wa": width_high, "wb": width_high, "wv": width_high, "wh": width_high,
        "ls": inputs.radius + vehicle.center_rear + inputs.threshold + 1.8,
        "w": width_high + 2 * inputs.radius,
        "d": inputs.radius + lane_seed + vehicle.center_rear + inputs.threshold + 3,
    }


@pytest.mark.parametrize("bidirectional", [False, True])
@pytest.mark.parametrize("extreme", [False, True])
@pytest.mark.parametrize("scenario", list(Scenario))
def test_no_solved_dimension_is_left_at_its_search_ceiling(
    scenario, extreme, bidirectional
):
    """A dimension stuck at the top of its bracket means the search found nothing, not

    that the road really has to be that wide. Two-way crossback used to report a 7.02 m
    turn-out road -- past its own 6.93 m ceiling, rescued there by the guard bump -- and
    the view presented it as solved.
    """

    inputs = _inputs(
        scenario=scenario, bidirectional=bidirectional, extreme=extreme,
        mode=SolveMode.FORWARD,
    )
    given = RoadDimensions()
    result = solve_scenario(inputs, given)
    assert not result.infeasible
    ceilings = _search_ceilings(inputs, given)
    for key in SOLVED_KEYS[scenario]:
        assert getattr(result.dims, key) < ceilings[key] - 1e-6, (key, result.dims)


def test_two_way_crossback_does_not_trade_the_whole_road_for_the_dip():
    """Regression: compressing one dimension at a time let whichever went first take the

    entire budget while the rest still sat at their ceilings, so the two-way turn-out road
    solved to 7.02 m beside a 0.36 m dip when the one-way answer -- 2.31 m and 2.72 m --
    fits the two-way layout too. Its region is the one-way one minus a wall and its second
    maneuver is an exact mirror, so two-way can never need a wider road than one-way here.
    """

    one_way, two_way = (
        solve_scenario(
            _inputs(
                scenario=Scenario.CROSSBACK, bidirectional=bidirectional,
                mode=SolveMode.FORWARD,
            ),
            RoadDimensions(),
        ).dims
        for bidirectional in (False, True)
    )
    assert two_way.wh == pytest.approx(one_way.wh, abs=2e-3)
    assert two_way.ls == pytest.approx(one_way.ls, abs=2e-3)
    assert two_way.wv == pytest.approx(one_way.wv, abs=2e-3)


@pytest.mark.parametrize("bidirectional", [False, True])
@pytest.mark.parametrize("scenario", list(Scenario))
def test_letting_the_truck_leave_the_centreline_never_widens_the_road(
    scenario, bidirectional
):
    """An extreme condition only adds freedom, so no dimension may come out larger.

    Every offset is free to stay at zero, which makes the centreline road feasible under
    the extreme condition too. Reporting a wider one says permission to leave the
    centreline forced a bigger road, which nobody can act on. Ten of the twelve variants
    used to do it -- two-way crossback asked 4.46 m of turn-out road against 2.51 m --
    because the offset optimiser takes a large offset early, while the road is still wide
    enough to make it free, and whichever dimension has to contain that offset is then
    stuck holding it.
    """

    solved = {}
    for extreme in (False, True):
        inputs = _inputs(
            scenario=scenario, bidirectional=bidirectional, extreme=extreme,
            mode=SolveMode.FORWARD,
        )
        result = solve_scenario(inputs, RoadDimensions())
        assert not result.infeasible, (scenario, bidirectional, extreme)
        solved[extreme] = result.dims
    for key in SOLVED_KEYS[scenario]:
        assert getattr(solved[True], key) <= getattr(solved[False], key) + 2e-3, (
            key, solved[False], solved[True]
        )


def test_stubback_starts_inside_the_branch_and_leaves_along_the_trunk():
    """The maneuver the requirement owner settled on, pinned end to end.

    Nose south inside the branch, one quarter turn in reverse onto the east side of the
    trunk, then forward away west. The shape it replaced never left the trunk at all: both
    of its arcs met on the trunk centreline, so the front axle bottomed out one radius down
    and only the tail ever swung into the branch.
    """

    inputs = _inputs(scenario=Scenario.STUBBACK, mode=SolveMode.FORWARD)
    result = solve_scenario(inputs, RoadDimensions())
    primitives = result.layout.maneuvers[0].primitives
    assert [item.gear for item in primitives] == [Gear.REVERSE, Gear.DRIVE]

    samples = sample_poses(primitives, DIMENSIONS, FINE)
    reach = np.where(samples.gear_is_drive, DIMENSIONS.center_front, -DIMENSIONS.center_front)
    nose_x, nose_y = samples.x + reach * samples.ux, samples.y + reach * samples.uy

    branch_floor = -result.dims.wh / 2 - result.dims.ls
    assert nose_y[0] < -result.dims.wh / 2, "起点车头必须已经在支路里"
    assert nose_y[0] == pytest.approx(samples.y[0] - DIMENSIONS.center_front, abs=1e-6)
    assert nose_y[0] > branch_floor, "车头不能穿出支路尽头"
    assert samples.x[0] == pytest.approx(0.0, abs=1e-6), "起点落在支路中线上"

    assert samples.x[-1] < samples.x[0], "驶出方向朝西"
    assert samples.y[-1] == pytest.approx(0.0, abs=1e-6), "终点回到主路中线"
    assert nose_x[-1] < samples.x[-1], "终点车头朝西"
    assert bool(samples.gear_is_drive[-1])

    turned = max(samples.x[: len(samples) // 2])
    assert turned > 0, "倒车转弯经过主路东侧"
