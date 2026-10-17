"""Deterministic helper↔person matcher + shelter selection.

The solver decides; the LLM only explains. Greedy priority matcher (scipy has
no py3.14 wheel): people are processed in a fixed priority order, each slot
takes the cheapest feasible helper with remaining capacity. Primaries for
everyone first, then backups, then shelters.
"""
from .generator import haversine_m, WALK_M_PER_MIN
from .models import (
    Assignment, AssignmentPlan, Evacuation, Helper, Intersection, Person, Shelter,
    TimeSlot, Uncovered,
)
from .routing import RoadGraph

MAX_ETA_MIN = 15.0
HELPER_CAP = 2
LIFT_SKILLS = {"carry", "wheelchair_assist"}
PREF_SKILL = {"oxygen": "nursing", "dialysis": "nursing", "dementia": "nursing",
              "infant": "first_aid", "hearing": "sign_language"}
MOBILITY_SEVERITY = {"bedridden": 0, "wheelchair": 1, "walker": 2, "walk": 3}


def needs_lift(p: Person) -> bool:
    return p.mobility in ("wheelchair", "bedridden")


def wheelchair_route(p: Person) -> bool:
    return p.mobility in ("wheelchair", "bedridden")


def helper_eligible(h: Helper, p: Person, slot: TimeSlot) -> bool:
    if not h.available or h.injured or slot not in h.availability:
        return False
    if needs_lift(p) and not (LIFT_SKILLS & set(h.skills)):
        return False
    return True


def pair_cost(h: Helper, p: Person, eta: float) -> float:
    lang_pen = 0.0 if p.language in h.languages else 6.0
    unmet = sum(1 for n in p.needs if n in PREF_SKILL and PREF_SKILL[n] not in h.skills)
    return eta + lang_pen + 3.0 * unmet + 5.0 * (1.0 - h.reliability)


def person_order(people: list[Person]) -> list[Person]:
    return sorted(people, key=lambda p: (-p.priority, -int(p.medical_power_dependent),
                                         MOBILITY_SEVERITY[p.mobility], p.id))


def _offset_min(lat: float, lon: float, it: Intersection) -> float:
    """Door-to-intersection walking time."""
    return haversine_m(lat, lon, it.lat, it.lon) / WALK_M_PER_MIN


def match_all(*, people: dict[str, Person], helpers: dict[str, Helper],
              shelters: dict[str, Shelter], rg: RoadGraph, slot: TimeSlot,
              version: int) -> AssignmentPlan:
    order = person_order(list(people.values()))
    load = {hid: 0 for hid in helpers}
    plan = AssignmentPlan(version=version, time_slot=slot)
    cands: dict[str, list[tuple[float, float, str]]] = {}
    reasons: dict[str, str] = {}
    h_off = {h.id: _offset_min(h.lat, h.lon, rg.ints[h.nearest_intersection]) for h in helpers.values()}

    # candidate lists (cost, eta, helper_id) per person
    for p in order:
        src = p.nearest_intersection
        dist = rg.dists_from(src, cutoff=MAX_ETA_MIN)
        p_off = _offset_min(p.lat, p.lon, rg.ints[src])
        eligible = [h for h in helpers.values() if helper_eligible(h, p, slot)]
        lst = []
        for h in eligible:
            d = dist.get(h.nearest_intersection)
            if d is None:
                continue
            eta = d + p_off + h_off[h.id]
            if eta <= MAX_ETA_MIN:
                lst.append((pair_cost(h, p, eta), round(eta, 1), h.id))
        lst.sort()
        cands[p.id] = lst
        if not eligible:
            reasons[p.id] = "no_candidates"
        elif not lst:
            reasons[p.id] = "eta_exceeded"

    # primary pass
    prim: dict[str, list[str]] = {p.id: [] for p in order}
    for p in order:
        for cost, eta, hid in cands[p.id]:
            if len(prim[p.id]) >= p.helpers_required:
                break
            if load[hid] < HELPER_CAP:
                load[hid] += 1
                prim[p.id].append(hid)
                plan.assignments.append(Assignment(helper_id=hid, person_id=p.id, role="primary",
                                                   eta_min=eta, plan_version=version))
        if len(prim[p.id]) < p.helpers_required and p.id not in reasons:
            reasons[p.id] = "capacity_exhausted"

    # backup pass
    for p in order:
        for cost, eta, hid in cands[p.id]:
            if hid not in prim[p.id] and load[hid] < HELPER_CAP:
                load[hid] += 1
                plan.assignments.append(Assignment(helper_id=hid, person_id=p.id, role="backup",
                                                   eta_min=eta, plan_version=version))
                break

    # shelters
    inbound = {s.id: 0 for s in shelters.values()}
    for p in order:
        ev = select_shelter(p, shelters, rg, inbound)
        if ev is None:
            reasons[p.id] = "unreachable"
            continue
        sid, path, eta = ev
        inbound[sid] += 1
        plan.evacuations.append(Evacuation(person_id=p.id, shelter_id=sid, route_ids=path,
                                           eta_min=eta, plan_version=version))

    for p in order:
        if p.id in reasons and (len(prim[p.id]) < p.helpers_required or reasons[p.id] == "unreachable"):
            plan.uncovered.append(Uncovered(person_id=p.id, priority=p.priority, reason=reasons[p.id]))
    return plan


def select_shelter(p: Person, shelters: dict[str, Shelter], rg: RoadGraph,
                   inbound: dict[str, int]) -> tuple[str, list[str], float] | None:
    wc = wheelchair_route(p)
    dist, paths = rg.paths_from(p.nearest_intersection, wheelchair=wc)
    prefers_welfare = p.priority >= 4 or p.mobility == "bedridden"

    def feasible(s: Shelter, check_cap: bool) -> bool:
        if s.damaged or s.nearest_intersection not in dist:
            return False
        if wc and not s.accessible:
            return False
        if p.medical_power_dependent and not s.has_generator:
            return False
        if check_cap and s.occupancy + inbound[s.id] >= s.capacity:
            return False
        return True

    for check_cap in (True, False):
        opts = [s for s in shelters.values() if feasible(s, check_cap)]
        if opts:
            def score(s: Shelter) -> float:
                bonus = 4.0 if (prefers_welfare and s.kind == "福祉避難所") else 0.0
                over = 0.0 if check_cap else (s.occupancy + inbound[s.id] - s.capacity)
                return dist[s.nearest_intersection] - bonus + over
            best = min(opts, key=score)
            return best.id, paths[best.nearest_intersection], round(dist[best.nearest_intersection], 1)
    # last resort for power-dependent: any reachable shelter beats none
    if p.medical_power_dependent:
        opts = [s for s in shelters.values() if not s.damaged and s.nearest_intersection in dist
                and (s.accessible or not wc)]
        if opts:
            best = min(opts, key=lambda s: dist[s.nearest_intersection])
            return best.id, paths[best.nearest_intersection], round(dist[best.nearest_intersection], 1)
    return None
