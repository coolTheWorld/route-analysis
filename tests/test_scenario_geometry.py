import math
from itertools import pairwise

import pytest

from route_analysis.models import VehicleDimensions
from route_analysis.scenario_geometry import (
    CAP_PAD,
    Arc,
    Gear,
    Line,
    Offsets,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    SolveMode,
    build_layout,
    dimension_label,
    mirror_primitive,
    variant_name,
)

DIMENSIONS = VehicleDimensions(width=1.23, center_front=1.545, center_rear=2.223)


def _inputs(**overrides) -> ScenarioInputs:
    base = {"dimensions": DIMENSIONS, "radius": 1.60, "threshold": 0.05}
    return ScenarioInputs(**(base | overrides))


@pytest.mark.parametrize("scenario", list(Scenario))
@pytest.mark.parametrize("bidirectional", [False, True])
def test_every_variant_builds_a_closed_region(scenario, bidirectional):
    layout = build_layout(
        _inputs(scenario=scenario, bidirectional=bidirectional),
        RoadDimensions(),
        Offsets(),
    )
    assert len(layout.region) >= 6
    assert len(layout.maneuvers) == (2 if bidirectional else 1)
    assert layout.centrelines


@pytest.mark.parametrize("scenario", list(Scenario))
def test_bidirectional_adds_the_mirror_of_the_first_maneuver(scenario):
    layout = build_layout(
        _inputs(scenario=scenario, bidirectional=True), RoadDimensions(), Offsets()
    )
    first, second = layout.maneuvers
    assert second.primitives == tuple(
        mirror_primitive(item) for item in first.primitives
    )


def test_mirror_flips_x_and_keeps_the_gear():
    line = mirror_primitive(Line((1.0, 2.0), (3.0, 4.0), Gear.REVERSE))
    assert line == Line((-1.0, 2.0), (-3.0, 4.0), Gear.REVERSE)
    arc = mirror_primitive(Arc((1.0, 2.0), 1.5, 0.0, math.pi / 2, Gear.DRIVE))
    assert arc.centre == (-1.0, 2.0)
    assert arc.start_angle == pytest.approx(math.pi)
    assert arc.end_angle == pytest.approx(math.pi / 2)


def test_primitives_join_end_to_end():
    """Primitives must join, or the sampled poses jump at every seam."""

    for scenario in Scenario:
        layout = build_layout(_inputs(scenario=scenario), RoadDimensions(), Offsets())
        for maneuver in layout.maneuvers:
            points = []
            for item in maneuver.primitives:
                if isinstance(item, Line):
                    points.append((item.start, item.end))
                else:
                    points.append(
                        (
                            (
                                item.centre[0] + item.radius * math.cos(item.start_angle),
                                item.centre[1] + item.radius * math.sin(item.start_angle),
                            ),
                            (
                                item.centre[0] + item.radius * math.cos(item.end_angle),
                                item.centre[1] + item.radius * math.sin(item.end_angle),
                            ),
                        )
                    )
            for (_, end), (start, _) in pairwise(points):
                assert math.dist(end, start) < 1e-9, scenario


def test_gear_swaps_the_overhangs_along_the_travel_direction():
    """Nose leads in D and tail leads in R, so the leg trims swap with the gear."""

    drive = build_layout(
        _inputs(scenario=Scenario.CORNER, gear=Gear.DRIVE), RoadDimensions(), Offsets()
    )
    reverse = build_layout(
        _inputs(scenario=Scenario.CORNER, gear=Gear.REVERSE), RoadDimensions(), Offsets()
    )
    drive_entry = drive.maneuvers[0].primitives[0].start[0]
    reverse_entry = reverse.maneuvers[0].primitives[0].start[0]
    assert drive_entry - reverse_entry == pytest.approx(
        DIMENSIONS.center_rear - DIMENSIONS.center_front
    )


def test_region_reaches_past_the_view_so_the_end_walls_never_bind():
    """The end walls exist only to close the polygon and must stay out of every pose's reach."""

    layout = build_layout(_inputs(scenario=Scenario.CORNER), RoadDimensions(), Offsets())
    left, bottom, right, top = layout.view_bounds
    xs = [point[0] for point in layout.region]
    ys = [point[1] for point in layout.region]
    assert min(xs) == pytest.approx(left - CAP_PAD)
    assert max(ys) == pytest.approx(top + CAP_PAD)
    assert max(xs) == pytest.approx(right)
    assert min(ys) == pytest.approx(bottom)


def test_uturn_reports_the_radius_it_can_actually_turn():
    layout = build_layout(
        _inputs(scenario=Scenario.UTURN), RoadDimensions(w=3.0, b=1.0), Offsets()
    )
    assert layout.turn_radius == pytest.approx(2.0)
    assert layout.buildable


def test_uturn_refuses_to_build_when_the_aisles_cannot_hold_the_radius():
    layout = build_layout(
        _inputs(scenario=Scenario.UTURN, radius=1.6),
        RoadDimensions(w=1.8, b=0.6),
        Offsets(),
    )
    assert not layout.buildable
    assert layout.radius_shortfall == pytest.approx(0.4)
    assert layout.maneuvers == ()


def test_uturn_offsets_move_both_legs_towards_the_divider():
    """Offsets are positive towards the inside, so both aisles lean on the divider."""

    plain = build_layout(_inputs(scenario=Scenario.UTURN), RoadDimensions(), Offsets())
    shifted = build_layout(
        _inputs(scenario=Scenario.UTURN), RoadDimensions(), Offsets(e1=0.2, e2=0.3)
    )
    assert shifted.turn_radius == pytest.approx(plain.turn_radius - 0.25)


def test_composite_scenarios_ignore_the_gear_selection():
    for scenario in (Scenario.CROSSBACK, Scenario.STUBBACK):
        inputs = _inputs(scenario=scenario, gear=Gear.REVERSE)
        assert inputs.gear_is_fixed
        assert inputs.effective_gear is Gear.DRIVE


def test_uturn_always_optimises_because_the_start_of_the_arc_is_free():
    assert _inputs(scenario=Scenario.UTURN, extreme=False).optimises_offsets
    assert not _inputs(scenario=Scenario.CORNER, extreme=False).optimises_offsets
    assert _inputs(scenario=Scenario.CORNER, extreme=True).optimises_offsets


def test_variant_names_match_the_sketch_list():
    assert (
        variant_name(_inputs(scenario=Scenario.CORNER, gear=Gear.REVERSE, extreme=True))
        == "单向直角R档转弯 · 极限工况（可不在道路中心线行驶）"
    )
    assert (
        variant_name(_inputs(scenario=Scenario.UTURN, bidirectional=True))
        == "双向D档U型转弯 · 车辆必须在道路中心线行驶"
    )
    assert (
        variant_name(_inputs(scenario=Scenario.STUBBACK, bidirectional=True))
        == "双向直角R档转弯转D档 · 车辆必须在道路中心线行驶"
    )


def test_dimension_labels_follow_the_traffic_direction():
    assert dimension_label(Scenario.CORNER, "wa", bidirectional=False) == "进入道宽"
    assert dimension_label(Scenario.CORNER, "wa", bidirectional=True) == "主路宽"


def test_radius_too_tight_is_flagged_against_half_the_body_width():
    assert _inputs(radius=0.6).radius_too_tight
    assert not _inputs(radius=1.6).radius_too_tight


def test_solve_modes_stay_distinct():
    assert _inputs(mode=SolveMode.CHECK).mode is SolveMode.CHECK
    assert _inputs().mode is SolveMode.FORWARD
