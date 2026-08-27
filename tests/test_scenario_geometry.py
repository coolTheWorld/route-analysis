import math
from itertools import pairwise

import pytest

from route_analysis.models import VehicleDimensions
from route_analysis.scenario_geometry import (
    CAP_PAD,
    NO_PINS,
    SOLVED_KEYS,
    Arc,
    Condition,
    Gear,
    Line,
    Offsets,
    Pins,
    RoadDimensions,
    Scenario,
    ScenarioInputs,
    build_layout,
    dimension_label,
    mirror_primitive,
    offset_label,
    offset_rows,
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
    # The entry leg now runs down the branch, so the trim shows on the y axis.
    drive_entry = drive.maneuvers[0].primitives[0].start[1]
    reverse_entry = reverse.maneuvers[0].primitives[0].start[1]
    assert drive_entry - reverse_entry == pytest.approx(
        DIMENSIONS.center_front - DIMENSIONS.center_rear
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


def test_stubback_branch_offset_is_positive_towards_the_arc_centre():
    """A positive ``so`` moves the branch leg east, towards the inside of the reverse turn.

    The arc centre sits east of the branch, so east is the inside, and the sign now follows
    the convention every other offset uses. It used to run the other way. The prototype
    fixture still carries the old sign, which is why the fixture loader negates it.
    """

    plain = build_layout(_inputs(scenario=Scenario.STUBBACK), RoadDimensions(), Offsets())
    shifted = build_layout(
        _inputs(scenario=Scenario.STUBBACK), RoadDimensions(), Offsets(so=0.3)
    )
    plain_arc, plain_line = plain.maneuvers[0].primitives
    shifted_arc, shifted_line = shifted.maneuvers[0].primitives
    assert isinstance(plain_arc, Arc) and isinstance(shifted_arc, Arc)
    assert isinstance(plain_line, Line) and isinstance(shifted_line, Line)
    assert plain_arc.centre[0] > 0.0, "圆心在支路东侧"
    assert shifted_arc.centre[0] - plain_arc.centre[0] == pytest.approx(0.3)
    assert shifted_line.start[0] - plain_line.start[0] == pytest.approx(0.3)
    branch_x = shifted_arc.centre[0] + shifted_arc.radius * math.cos(shifted_arc.start_angle)
    assert branch_x == pytest.approx(0.3), "支路腿本身向东移了 so"


def test_a_two_way_stub_branch_is_held_on_its_centreline():
    """Both mirrored maneuvers start in the one branch, so it cannot lean either way."""

    plain = build_layout(
        _inputs(scenario=Scenario.STUBBACK, bidirectional=True), RoadDimensions(), Offsets()
    )
    shifted = build_layout(
        _inputs(scenario=Scenario.STUBBACK, bidirectional=True),
        RoadDimensions(),
        Offsets(so=0.3),
    )
    assert shifted.maneuvers == plain.maneuvers


def test_composite_scenarios_ignore_the_gear_selection():
    for scenario in (Scenario.CROSSBACK, Scenario.STUBBACK):
        inputs = _inputs(scenario=scenario, gear=Gear.REVERSE)
        assert inputs.gear_is_fixed
        assert inputs.effective_gear is Gear.DRIVE


def test_uturn_always_optimises_because_the_start_of_the_arc_is_free():
    assert _inputs(scenario=Scenario.UTURN, condition=Condition.CENTRELINE).optimises_offsets
    assert not _inputs(scenario=Scenario.CORNER, condition=Condition.CENTRELINE).optimises_offsets
    assert _inputs(scenario=Scenario.CORNER, condition=Condition.PARETO).optimises_offsets


def test_variant_names_match_the_sketch_list():
    assert (
        variant_name(
            _inputs(scenario=Scenario.CORNER, gear=Gear.REVERSE, condition=Condition.PARETO)
        )
        == "单向直角R档转弯 · 帕累托极限（可不在道路中心线行驶）"
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
    assert dimension_label(Scenario.CORNER, "wa", bidirectional=False) == "驶出道宽"
    assert dimension_label(Scenario.CORNER, "wa", bidirectional=True) == "主路宽"


def test_radius_too_tight_is_flagged_against_half_the_body_width():
    assert _inputs(radius=0.6).radius_too_tight
    assert not _inputs(radius=1.6).radius_too_tight


def test_the_two_conditions_stay_distinct():
    assert list(Condition) == [Condition.CENTRELINE, Condition.PARETO]
    assert _inputs().condition is Condition.CENTRELINE
    assert not _inputs().pareto
    assert _inputs(condition=Condition.PARETO).pareto


@pytest.mark.parametrize(
    ("scenario", "bidirectional", "expected"),
    [
        (Scenario.CORNER, False, [("ea", False), ("eb", False)]),
        (Scenario.CORNER, True, [("ea", False), ("eb", True)]),
        (Scenario.CROSSBACK, False, [("ev", False), ("eh", False)]),
        (Scenario.CROSSBACK, True, [("ev", True), ("eh", False)]),
        (Scenario.STUBBACK, False, [("a", False), ("so", False)]),
        (Scenario.STUBBACK, True, [("a", False), ("so", True)]),
        (Scenario.UTURN, False, [("e1", False), ("e2", False), ("yc", False)]),
        (Scenario.UTURN, True, [(None, True), ("eo", False), ("yc", False)]),
    ],
)
def test_offset_rows_list_every_leg_and_flag_the_shared_one(
    scenario, bidirectional, expected
):
    """Two rows per junction, three per U-turn; a two-way layout marks its shared leg."""

    rows = offset_rows(scenario, bidirectional=bidirectional)
    assert [(row.key, row.shared) for row in rows] == expected
    assert all(row.label for row in rows)


def test_the_two_way_uturn_middle_aisle_has_a_row_but_no_variable():
    first, *_ = offset_rows(Scenario.UTURN, bidirectional=True)
    assert first.key is None
    assert first.shared
    assert first.label == "中巷偏移"
    assert all(row.key is not None for row in offset_rows(Scenario.UTURN, bidirectional=False))


def test_offset_labels_follow_the_traffic_direction():
    assert offset_label(Scenario.CORNER, "ea", bidirectional=False) == "驶出道偏移"
    assert offset_label(Scenario.CORNER, "ea", bidirectional=True) == "主路偏移"
    assert offset_label(Scenario.CORNER, "eb", bidirectional=False) == "进入道偏移"
    assert offset_label(Scenario.CORNER, "eb", bidirectional=True) == "支路偏移"
    assert offset_label(Scenario.UTURN, "yc", bidirectional=True) == "起弯点"
    # A key the layout does not list falls back to the generic name, then to the key.
    assert offset_label(Scenario.UTURN, "eo", bidirectional=False) == "外侧巷道偏移"
    assert offset_label(Scenario.CORNER, "zz", bidirectional=False) == "zz"


@pytest.mark.parametrize("scenario", list(Scenario))
def test_pinning_every_dimension_pins_exactly_the_solved_ones(scenario):
    pins = Pins.all_dims(scenario)
    assert pins.dims == frozenset(SOLVED_KEYS[scenario])
    assert pins.offsets == frozenset()
    assert "b" not in Pins.all_dims(Scenario.UTURN).dims, "隔墙宽从来是给定项"


def test_pins_toggle_one_key_at_a_time_without_touching_the_original():
    pinned = NO_PINS.with_dim("wa", True)
    assert pinned.dims == {"wa"}
    assert NO_PINS.dims == frozenset(), "frozen: 原对象不受影响"
    assert pinned.with_dim("wa", False) == NO_PINS
    assert pinned.with_dim("wb", False) == pinned, "释放没固定的键是空操作"
    with_offsets = pinned.with_offset("ea", True).with_offset("yc", True)
    assert with_offsets.dims == {"wa"}
    assert with_offsets.offsets == {"ea", "yc"}
    assert with_offsets.with_offset("ea", False).offsets == {"yc"}


def test_the_start_of_the_arc_does_not_count_as_a_lateral_offset():
    assert Pins(offsets=frozenset({"yc", "eo"})).lateral_offsets == {"eo"}
    assert Pins(offsets=frozenset({"yc"})).lateral_offsets == frozenset()
    assert NO_PINS.lateral_offsets == frozenset()
