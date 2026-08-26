"""Drivable region and maneuver path for each of the four rapid-estimate scenarios.

Geometry only, no solving: given the vehicle, the road dimensions and the lateral
offsets, hand back the rectilinear polygon the truck must stay inside, the one or two
maneuvers it drives, and the centrelines the plan view draws. Scene y points up,
lengths are metres and angles radians.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum

from route_analysis.models import VehicleDimensions


class Scenario(StrEnum):
    CORNER = "corner"
    CROSSBACK = "crossback"
    STUBBACK = "stubback"
    UTURN = "uturn"


class Gear(StrEnum):
    """Gear is pure geometry here: it only swaps the overhangs along the travel direction."""

    DRIVE = "D"
    REVERSE = "R"


class Condition(StrEnum):
    """How the truck may sit in the road; the one picker that replaced 计算方向 and 工况.

    ``CENTRELINE`` keeps every lateral offset at zero and solves the road. ``PARETO`` frees
    the offsets and, on top of that, lets the operator pin any dimension or offset: what is
    pinned is taken as given, everything else is driven to its limit.
    """

    CENTRELINE = "centreline"
    PARETO = "pareto"


SCENARIO_NAMES: dict[Scenario, str] = {
    Scenario.CORNER: "直角转弯",
    Scenario.CROSSBACK: "直角R档直行转D档",
    Scenario.STUBBACK: "直角R档转弯转D档",
    Scenario.UTURN: "U型转弯",
}

SCENARIO_SUBTITLES: dict[Scenario, str] = {
    Scenario.CORNER: "L 口 / T 口",
    Scenario.CROSSBACK: "倒车穿越再转出",
    Scenario.STUBBACK: "自支路倒车汇入",
    Scenario.UTURN: "端头掉头",
}

FIXED_GEAR_SCENARIOS = (Scenario.CROSSBACK, Scenario.STUBBACK)
"""Both of these pin the gear to R then D, so the gear picker cannot change the solve."""

SOLVED_KEYS: dict[Scenario, tuple[str, ...]] = {
    Scenario.CORNER: ("wa", "wb"),
    Scenario.CROSSBACK: ("wv", "wh", "ls"),
    Scenario.STUBBACK: ("wh", "wv"),
    Scenario.UTURN: ("w", "d"),
}

DIMENSION_LABELS: dict[Scenario, dict[str, tuple[str, str]]] = {
    Scenario.CORNER: {
        "wa": ("进入道宽", "主路宽"),
        "wb": ("驶出道宽", "支路宽"),
    },
    Scenario.CROSSBACK: {
        "wv": ("倒车道宽", "倒车道宽"),
        "wh": ("转出道宽", "转出道宽"),
        "ls": ("倒车下探长度", "倒车下探长度"),
    },
    Scenario.STUBBACK: {
        "wh": ("主路宽", "主路宽"),
        "wv": ("支路宽", "支路宽"),
    },
    Scenario.UTURN: {
        "w": ("巷道宽", "巷道宽"),
        "b": ("隔墙宽", "隔墙宽"),
        "d": ("端头深度", "端头深度"),
    },
}


def dimension_label(scenario: Scenario, key: str, *, bidirectional: bool) -> str:
    """Names differ by traffic direction: an L junction entry leg is a T junction trunk."""

    names = DIMENSION_LABELS[scenario].get(key)
    if names is None:
        return key
    return names[1] if bidirectional else names[0]


Point = tuple[float, float]

CAP_PAD = 6.0
"""How far past the view the open ends of a road are walled off.

The polygon has to close, but the road really does run on in those directions, so those
two or three edges are an artefact. Pushing them out of reach leaves the clearance
decided by real walls only. Leave them where the path starts and the reported clearance
pins to the path trim instead (a flat 0.15 m in the prototype), and any threshold above
that makes every scenario permanently infeasible.
"""


@dataclass(frozen=True, slots=True)
class ScenarioInputs:
    """Everything one estimate needs apart from the road dimensions and the offsets."""

    scenario: Scenario = Scenario.CORNER
    condition: Condition = Condition.CENTRELINE
    bidirectional: bool = False
    gear: Gear = Gear.DRIVE
    dimensions: VehicleDimensions = field(
        default_factory=lambda: VehicleDimensions(
            width=1.23, center_front=1.545, center_rear=2.223
        )
    )
    radius: float = 1.20
    threshold: float = 0.15

    @property
    def effective_gear(self) -> Gear:
        if self.scenario in FIXED_GEAR_SCENARIOS:
            return Gear.DRIVE
        return self.gear

    @property
    def gear_is_fixed(self) -> bool:
        return self.scenario in FIXED_GEAR_SCENARIOS

    @property
    def pareto(self) -> bool:
        return self.condition is Condition.PARETO

    @property
    def optimises_offsets(self) -> bool:
        """The U-turn start point is free under every condition, so it always optimises."""

        return self.pareto or self.scenario is Scenario.UTURN

    @property
    def radius_too_tight(self) -> bool:
        """Below half a body width plus the threshold the inner geometry self-intersects."""

        return self.radius <= self.dimensions.width / 2 + self.threshold


@dataclass(frozen=True, slots=True)
class RoadDimensions:
    """Every road dimension; each scenario reads only the two or three it needs."""

    wa: float = 3.00
    wb: float = 3.00
    wv: float = 3.00
    wh: float = 3.50
    ls: float = 4.00
    w: float = 3.00
    b: float = 1.00
    d: float = 4.50

    def with_value(self, key: str, value: float) -> RoadDimensions:
        return replace(self, **{key: value})


@dataclass(frozen=True, slots=True)
class Offsets:
    """Lateral offset per leg, positive towards the inside of the turn. ``yc`` is the

    longitudinal start of the U-turn arc rather than a lateral shift. Keys a layout does
    not use are ignored by its builder, and a two-way layout holds its shared leg at zero
    whatever value sits here.
    """

    ea: float = 0.0
    eb: float = 0.0
    ev: float = 0.0
    eh: float = 0.0
    a: float = 0.0
    so: float = 0.0
    e1: float = 0.0
    e2: float = 0.0
    eo: float = 0.0
    yc: float = 0.0

    def with_value(self, key: str, value: float) -> Offsets:
        return replace(self, **{key: value})


CENTRED = Offsets()
"""Every leg on its centreline and the U-turn arc starting at the aisle mouth."""

OFFSET_LABELS: dict[str, str] = {
    "ea": "进入道偏移",
    "eb": "驶出道偏移",
    "ev": "倒车道偏移",
    "eh": "转出道偏移",
    "a": "主路偏移",
    "so": "支路偏移",
    "e1": "上行巷道偏移",
    "e2": "下行巷道偏移",
    "eo": "外侧巷道偏移",
    "yc": "起弯点",
}


@dataclass(frozen=True, slots=True)
class Pins:
    """Which road dimensions and offsets the operator has taken over from the solver.

    Only the Pareto condition reads them. A pinned key keeps the value the operator typed;
    every other solved dimension is driven to its limit around it and every other offset is
    optimised. Shared-leg offsets in a two-way layout are never pinned: geometry holds them
    at zero regardless.
    """

    dims: frozenset[str] = frozenset()
    offsets: frozenset[str] = frozenset()

    @classmethod
    def all_dims(cls, scenario: Scenario) -> Pins:
        """Every solved dimension pinned: nothing left to solve, the road is simply checked."""

        return cls(dims=frozenset(SOLVED_KEYS[scenario]))

    def with_dim(self, key: str, pinned: bool) -> Pins:
        dims = self.dims | {key} if pinned else self.dims - {key}
        return replace(self, dims=frozenset(dims))

    def with_offset(self, key: str, pinned: bool) -> Pins:
        offsets = self.offsets | {key} if pinned else self.offsets - {key}
        return replace(self, offsets=frozenset(offsets))

    @property
    def lateral_offsets(self) -> frozenset[str]:
        """The pinned offsets that move a leg sideways; ``yc`` slides along the aisle."""

        return self.offsets - {"yc"}


NO_PINS = Pins()
"""Nothing pinned; the default the solver reads when no operator input is involved."""


@dataclass(frozen=True, slots=True)
class OffsetRow:
    """One offset as the road-parameter column lists it.

    ``key`` is ``None`` for the two-way U-turn middle aisle: both maneuvers share it and no
    variable stands behind it, but the row is still listed so the layout reads complete.
    ``shared`` rows are held at zero by geometry and are shown disabled.
    """

    key: str | None
    label: str
    shared: bool = False


def offset_rows(scenario: Scenario, *, bidirectional: bool) -> tuple[OffsetRow, ...]:
    """Every leg's offset for this layout, in the order the sidebar lists them.

    Two-way layouts share one leg between the mirrored maneuvers, and a leg that carries
    both cannot lean one way for one and the other way for the other, so that row is fixed
    at zero. Names follow ``dimension_label``: an L junction's entry leg is a T junction's
    trunk.
    """

    if scenario is Scenario.CORNER:
        if bidirectional:
            return (OffsetRow("ea", "主路偏移"), OffsetRow("eb", "支路偏移", shared=True))
        return (OffsetRow("ea", "进入道偏移"), OffsetRow("eb", "驶出道偏移"))
    if scenario is Scenario.CROSSBACK:
        return (
            OffsetRow("ev", "倒车道偏移", shared=bidirectional),
            OffsetRow("eh", "转出道偏移"),
        )
    if scenario is Scenario.STUBBACK:
        return (OffsetRow("a", "主路偏移"), OffsetRow("so", "支路偏移", shared=bidirectional))
    if bidirectional:
        return (
            OffsetRow(None, "中巷偏移", shared=True),
            OffsetRow("eo", "外侧巷道偏移"),
            OffsetRow("yc", "起弯点"),
        )
    return (
        OffsetRow("e1", "上行巷道偏移"),
        OffsetRow("e2", "下行巷道偏移"),
        OffsetRow("yc", "起弯点"),
    )


def offset_label(scenario: Scenario, key: str, *, bidirectional: bool) -> str:
    for row in offset_rows(scenario, bidirectional=bidirectional):
        if row.key == key:
            return row.label
    return OFFSET_LABELS.get(key, key)


@dataclass(frozen=True, slots=True)
class Line:
    start: Point
    end: Point
    gear: Gear


@dataclass(frozen=True, slots=True)
class Arc:
    centre: Point
    radius: float
    start_angle: float
    end_angle: float
    gear: Gear


Primitive = Line | Arc


@dataclass(frozen=True, slots=True)
class Maneuver:
    label: str
    primitives: tuple[Primitive, ...]


@dataclass(frozen=True, slots=True)
class ScenarioLayout:
    """Geometry ready for the clearance engine: region, maneuvers and drawing guides."""

    region: tuple[Point, ...]
    maneuvers: tuple[Maneuver, ...]
    centrelines: tuple[tuple[Point, Point], ...]
    view_bounds: tuple[float, float, float, float]
    """What the plan view frames; the polygon itself runs far past this on the open ends."""

    extents: dict[str, float]
    turn_radius: float | None = None
    radius_shortfall: float = 0.0

    @property
    def buildable(self) -> bool:
        """A U-turn narrower than the minimum turning radius has no path to draw at all."""

        return self.radius_shortfall <= 0.0


def mirror_primitive(primitive: Primitive) -> Primitive:
    """Reflect one primitive about x = 0, which is how a two-way layout gets its twin."""

    if isinstance(primitive, Line):
        return Line(
            (-primitive.start[0], primitive.start[1]),
            (-primitive.end[0], primitive.end[1]),
            primitive.gear,
        )
    return Arc(
        (-primitive.centre[0], primitive.centre[1]),
        primitive.radius,
        math.pi - primitive.start_angle,
        math.pi - primitive.end_angle,
        primitive.gear,
    )


def _mirror(primitives: tuple[Primitive, ...]) -> tuple[Primitive, ...]:
    return tuple(mirror_primitive(item) for item in primitives)


def _overhangs(gear: Gear, dimensions: VehicleDimensions) -> tuple[float, float]:
    """Leading and trailing overhang along travel: nose first in D, tail first in R."""

    if gear is Gear.DRIVE:
        return dimensions.center_front, dimensions.center_rear
    return dimensions.center_rear, dimensions.center_front


def _corner(
    inputs: ScenarioInputs, dims: RoadDimensions, offsets: Offsets
) -> ScenarioLayout:
    vehicle = inputs.dimensions
    radius = inputs.radius
    wa, wb = dims.wa, dims.wb
    ea = offsets.ea
    eb = 0.0 if inputs.bidirectional else offsets.eb
    span = radius + vehicle.center_front + vehicle.center_rear
    la = wb / 2 + span + 1.1
    lb = wa / 2 + span + 1.1
    lax = la + CAP_PAD
    lbx = lb + CAP_PAD
    region: tuple[Point, ...]
    centrelines: tuple[tuple[Point, Point], ...]
    if inputs.bidirectional:
        region = (
            (-lax, -wa / 2), (lax, -wa / 2), (lax, wa / 2), (wb / 2, wa / 2),
            (wb / 2, lbx), (-wb / 2, lbx), (-wb / 2, wa / 2), (-lax, wa / 2),
        )
        centrelines = (((-la, 0.0), (la, 0.0)), ((0.0, -wa / 2), (0.0, lb)))
        view = (-la, -wa / 2, la, lb)
    else:
        region = (
            (-lax, -wa / 2), (wb / 2, -wa / 2), (wb / 2, lbx),
            (-wb / 2, lbx), (-wb / 2, wa / 2), (-lax, wa / 2),
        )
        centrelines = (((-la, 0.0), (wb / 2, 0.0)), ((0.0, -wa / 2), (0.0, lb)))
        view = (-la, -wa / 2, wb / 2, lb)
    gear = inputs.effective_gear
    lead, trail = _overhangs(gear, vehicle)
    ay = ea
    bx = -eb
    primitives = (
        Line((-la + trail + 0.15, ay), (bx - radius, ay), gear),
        Arc((bx - radius, ay + radius), radius, -math.pi / 2, 0.0, gear),
        Line((bx, ay + radius), (bx, lb - lead - 0.15), gear),
    )
    maneuvers = [Maneuver("西侧进入" if inputs.bidirectional else "转弯段", primitives)]
    if inputs.bidirectional:
        maneuvers.append(Maneuver("东侧进入", _mirror(primitives)))
    return ScenarioLayout(
        region=region,
        maneuvers=tuple(maneuvers),
        centrelines=centrelines,
        view_bounds=view,
        extents={"la": la, "lb": lb},
    )


def _crossback(
    inputs: ScenarioInputs, dims: RoadDimensions, offsets: Offsets
) -> ScenarioLayout:
    vehicle = inputs.dimensions
    radius = inputs.radius
    wv, wh, ls = dims.wv, dims.wh, dims.ls
    ev = 0.0 if inputs.bidirectional else offsets.ev
    eh = offsets.eh
    length = vehicle.center_front + vehicle.center_rear
    ln = wh / 2 + length + 0.9
    lw = wv / 2 + radius + length + 1.1
    lnx = ln + CAP_PAD
    lwx = lw + CAP_PAD
    region: tuple[Point, ...]
    horizontal: tuple[Point, Point]
    if inputs.bidirectional:
        region = (
            (-lwx, -wh / 2), (-wv / 2, -wh / 2), (-wv / 2, -wh / 2 - ls),
            (wv / 2, -wh / 2 - ls), (wv / 2, -wh / 2), (lwx, -wh / 2),
            (lwx, wh / 2), (wv / 2, wh / 2), (wv / 2, lnx),
            (-wv / 2, lnx), (-wv / 2, wh / 2), (-lwx, wh / 2),
        )
        horizontal = ((-lw, 0.0), (lw, 0.0))
        view = (-lw, -wh / 2 - ls, lw, ln)
    else:
        region = (
            (-lwx, -wh / 2), (-wv / 2, -wh / 2), (-wv / 2, -wh / 2 - ls),
            (wv / 2, -wh / 2 - ls), (wv / 2, lnx), (-wv / 2, lnx),
            (-wv / 2, wh / 2), (-lwx, wh / 2),
        )
        horizontal = ((-lw, 0.0), (wv / 2, 0.0))
        view = (-lw, -wh / 2 - ls, wv / 2, ln)
    sx = -ev
    ey = -eh
    y0 = ey - radius
    primitives = (
        Line((sx, ln - vehicle.center_front - 0.15), (sx, y0), Gear.REVERSE),
        Arc((sx - radius, y0), radius, 0.0, math.pi / 2, Gear.DRIVE),
        Line((sx - radius, ey), (-lw + vehicle.center_front + 0.15, ey), Gear.DRIVE),
    )
    maneuvers = [Maneuver("向西转出", primitives)]
    if inputs.bidirectional:
        maneuvers.append(Maneuver("向东转出", _mirror(primitives)))
    return ScenarioLayout(
        region=region,
        maneuvers=tuple(maneuvers),
        centrelines=(((0.0, -wh / 2 - ls), (0.0, ln)), horizontal),
        view_bounds=view,
        extents={"lw": lw, "ln": ln},
    )


def _stubback(
    inputs: ScenarioInputs, dims: RoadDimensions, offsets: Offsets
) -> ScenarioLayout:
    vehicle = inputs.dimensions
    radius = inputs.radius
    wh, wv = dims.wh, dims.wv
    ry = -offsets.a
    # Positive towards the inside of the turn, like every other offset: the arc centre
    # sits east of the branch leg, so a positive branch offset moves the leg east.
    sx = 0.0 if inputs.bidirectional else offsets.so
    length = vehicle.center_front + vehicle.center_rear
    lx = abs(sx) + radius + length + 1.2
    lxx = lx + CAP_PAD
    # The branch carries on past the parked truck, so its far end is pushed out of reach
    # like the open ends of the trunk. How much deeper it runs constrains nothing.
    floor = -wh / 2 - CAP_PAD
    region = (
        (-lxx, -wh / 2), (-wv / 2, -wh / 2), (-wv / 2, floor),
        (wv / 2, floor), (wv / 2, -wh / 2), (lxx, -wh / 2),
        (lxx, wh / 2), (-lxx, wh / 2),
    )
    reach = length + 0.8
    view_floor = min(-wh / 2, ry - radius - vehicle.center_front) - 0.8
    primitives = (
        Arc((sx + radius, ry - radius), radius, math.pi, math.pi / 2, Gear.REVERSE),
        Line((sx + radius, ry), (sx - radius - reach, ry), Gear.DRIVE),
    )
    maneuvers = [Maneuver("向西驶出", primitives)]
    if inputs.bidirectional:
        maneuvers.append(Maneuver("向东驶出", _mirror(primitives)))
    return ScenarioLayout(
        region=region,
        maneuvers=tuple(maneuvers),
        centrelines=(((-lx, 0.0), (lx, 0.0)), ((0.0, view_floor), (0.0, wh / 2))),
        view_bounds=(-lx, view_floor, lx, wh / 2),
        extents={"lx": lx},
    )


def _uturn(
    inputs: ScenarioInputs, dims: RoadDimensions, offsets: Offsets
) -> ScenarioLayout:
    vehicle = inputs.dimensions
    radius = inputs.radius
    w, b, depth = dims.w, dims.b, dims.d
    gear = inputs.effective_gear
    lead, trail = _overhangs(gear, vehicle)
    ld = vehicle.center_front + vehicle.center_rear + 2.4
    ldx = ld + CAP_PAD
    pitch = w + b
    region: tuple[Point, ...]
    centrelines: tuple[tuple[Point, Point], ...]
    primary: str
    mirrored: str
    if inputs.bidirectional:
        eo = offsets.eo
        # Both maneuvers set off up the shared middle aisle and come back down an outer
        # one. Running it the other way round would have the two of them finishing nose to
        # nose in the aisle they share.
        ascend = 0.0
        descend = -pitch + eo
        outer = 1.5 * w + b
        region = (
            (-outer, -ldx), (-w / 2 - b, -ldx), (-w / 2 - b, 0.0), (-w / 2, 0.0),
            (-w / 2, -ldx), (w / 2, -ldx), (w / 2, 0.0), (w / 2 + b, 0.0),
            (w / 2 + b, -ldx), (outer, -ldx), (outer, depth), (-outer, depth),
        )
        centrelines = (
            ((-pitch, -ld), (-pitch, 0.4)),
            ((0.0, -ld), (0.0, 0.4)),
            ((pitch, -ld), (pitch, 0.4)),
        )
        primary, mirrored = "左侧掉头", "右侧掉头"
    else:
        ascend = -pitch / 2 + offsets.e1
        descend = pitch / 2 - offsets.e2
        outer = pitch / 2 + w / 2
        region = (
            (-outer, -ldx), (-b / 2, -ldx), (-b / 2, 0.0), (b / 2, 0.0),
            (b / 2, -ldx), (outer, -ldx), (outer, depth), (-outer, depth),
        )
        centrelines = (
            ((-pitch / 2, -ld), (-pitch / 2, 0.4)),
            ((pitch / 2, -ld), (pitch / 2, 0.4)),
        )
        primary, mirrored = "掉头", "掉头"
    view = (-outer, -ld, outer, depth)
    turn_radius = abs(descend - ascend) / 2
    extents = {"ld": ld, "outer": outer}
    if turn_radius < radius - 1e-9:
        return ScenarioLayout(
            region=region,
            maneuvers=(),
            centrelines=centrelines,
            view_bounds=view,
            extents=extents,
            turn_radius=turn_radius,
            radius_shortfall=radius - turn_radius,
        )
    yc = offsets.yc
    # The half circle always goes over the top; which end it starts from is whichever side
    # the climbing aisle sits on.
    climbing_left = ascend < descend
    primitives = (
        Line((ascend, -ld + trail + 0.15), (ascend, yc), gear),
        Arc(
            ((ascend + descend) / 2, yc),
            turn_radius,
            math.pi if climbing_left else 0.0,
            0.0 if climbing_left else math.pi,
            gear,
        ),
        Line((descend, yc), (descend, -ld + lead + 0.15), gear),
    )
    maneuvers = [Maneuver(primary, primitives)]
    if inputs.bidirectional:
        maneuvers.append(Maneuver(mirrored, _mirror(primitives)))
    return ScenarioLayout(
        region=region,
        maneuvers=tuple(maneuvers),
        centrelines=centrelines,
        view_bounds=view,
        extents=extents,
        turn_radius=turn_radius,
    )


_BUILDERS = {
    Scenario.CORNER: _corner,
    Scenario.CROSSBACK: _crossback,
    Scenario.STUBBACK: _stubback,
    Scenario.UTURN: _uturn,
}


def build_layout(
    inputs: ScenarioInputs, dims: RoadDimensions, offsets: Offsets
) -> ScenarioLayout:
    """Build the drivable region and the maneuver path for one scenario."""

    return _BUILDERS[inputs.scenario](inputs, dims, offsets)


def variant_name(inputs: ScenarioInputs) -> str:
    """Full variant name for the plan-view heading, one per line of the customer sketches."""

    direction = "双向" if inputs.bidirectional else "单向"
    if inputs.scenario is Scenario.CORNER:
        core = f"直角{inputs.effective_gear.value}档转弯"
    elif inputs.scenario is Scenario.CROSSBACK:
        core = "直角R档直行转D档"
    elif inputs.scenario is Scenario.STUBBACK:
        core = "直角R档转弯转D档"
    else:
        core = f"{inputs.effective_gear.value}档U型转弯"
    condition = (
        "帕累托极限（可不在道路中心线行驶）"
        if inputs.pareto
        else "车辆必须在道路中心线行驶"
    )
    return f"{direction}{core} · {condition}"
