"""Gap analysis: daytime slot gap, single points of failure, critical roads,
shelter overflow. Computed on the in-memory mirror (instant); the same
questions are also answerable in Cypher on Aura (see CYPHER below)."""
import math

from .matcher import match_all, select_shelter, person_order
from .metrics import compute_coverage, person_statuses
from .models import AssignmentPlan, Helper, Person, Road, Shelter
from .routing import RoadGraph


def slot_gap(people, helpers, shelters, rg) -> dict:
    res = {}
    for slot in ("weekday_night", "weekday_day"):
        plan = match_all(people=people, helpers=helpers, shelters=shelters, rg=rg, slot=slot, version=0)
        res[slot] = (plan, person_statuses(people, plan), compute_coverage(people, shelters, plan))
    night_st, day_st = res["weekday_night"][1], res["weekday_day"][1]
    day_plan = res["weekday_day"][0]
    prim_day: dict[str, int] = {}
    for a in day_plan.assignments:
        if a.role == "primary":
            prim_day[a.person_id] = prim_day.get(a.person_id, 0) + 1
    rows, by_block = [], {}
    for p in person_order(list(people.values())):
        if night_st[p.id] in ("covered", "partial") and day_st[p.id] == "uncovered" or (
                prim_day.get(p.id, 0) < p.helpers_required and night_st[p.id] != "uncovered"):
            missing = p.helpers_required - prim_day.get(p.id, 0)
            rows.append({"person_id": p.id, "block": p.block, "priority": p.priority,
                         "needs": p.needs, "night": night_st[p.id], "day": day_st[p.id], "missing": missing})
            b = by_block.setdefault(p.block, {"block": p.block, "people": 0, "missing_slots": 0})
            b["people"] += 1
            b["missing_slots"] += missing
    blocks = sorted(by_block.values(), key=lambda b: -b["missing_slots"])
    for b in blocks:
        b["recruit"] = math.ceil(b["missing_slots"] / 2)  # each helper covers up to 2
    return {"people": rows, "blocks": blocks,
            "night_pct": res["weekday_night"][2].pct_covered, "day_pct": res["weekday_day"][2].pct_covered}


def spof(people: dict[str, Person], helpers: dict[str, Helper], plan: AssignmentPlan) -> list[dict]:
    has_backup = {a.person_id for a in plan.assignments if a.role == "backup"}
    out: dict[str, dict] = {}
    for a in plan.assignments:
        if a.role != "primary":
            continue
        p = people[a.person_id]
        row = out.setdefault(a.helper_id, {"helper_id": a.helper_id, "name": helpers[a.helper_id].display_name,
                                           "role": helpers[a.helper_id].role, "stranded_if_lost": [], "high_priority": 0})
        if a.person_id not in has_backup:
            row["stranded_if_lost"].append(a.person_id)
        if p.priority >= 4:
            row["high_priority"] += 1
    rows = [r for r in out.values() if r["stranded_if_lost"] or r["high_priority"] >= 2]
    return sorted(rows, key=lambda r: (-len(r["stranded_if_lost"]), -r["high_priority"]))[:15]


def critical_roads(people, shelters, ints, roads: dict[str, Road], plan: AssignmentPlan) -> list[dict]:
    seg_users: dict[str, list[tuple[str, float]]] = {}
    base = RoadGraph(ints, roads)
    for e in plan.evacuations:
        p = people[e.person_id]
        rr = base.route(p.nearest_intersection, shelters[e.shelter_id].nearest_intersection,
                        wheelchair=p.mobility in ("wheelchair", "bedridden"))
        if rr is None:
            continue
        for rid in rr.road_ids:
            seg = rid[:-1] if rid.endswith("r") else rid
            seg_users.setdefault(seg, []).append((e.person_id, e.eta_min))
    rows = []
    for seg, users in seg_users.items():
        if len(users) < 2:
            continue
        mod = dict(roads)
        for rid in (seg, seg + "r"):
            mod[rid] = roads[rid].model_copy(update={"blocked": True})
        rg = RoadGraph(ints, mod)
        stranded, delayed = [], []
        for pid, eta in users:
            ev = select_shelter(people[pid], shelters, rg, {s: 0 for s in shelters})
            if ev is None:
                stranded.append(pid)
            elif ev[2] - eta >= 2.0:
                delayed.append(pid)
        if stranded or delayed:
            r = roads[seg]
            rows.append({"road_id": seg, "from": r.from_id, "to": r.to_id, "width_m": r.width_m,
                         "block_wall_risk": r.block_wall_risk, "users": len(users),
                         "strands": stranded, "delays": delayed})
    return sorted(rows, key=lambda r: (-len(r["strands"]), -len(r["delays"]), -r["users"]))[:12]


def shelter_overflow(shelters: dict[str, Shelter], plan: AssignmentPlan) -> list[dict]:
    inbound: dict[str, int] = {}
    for e in plan.evacuations:
        inbound[e.shelter_id] = inbound.get(e.shelter_id, 0) + 1
    rows = [{"shelter_id": s.id, "name": s.name, "kind": s.kind, "capacity": s.capacity,
             "occupancy": s.occupancy, "inbound": inbound.get(s.id, 0), "damaged": s.damaged,
             "overflow": s.occupancy + inbound.get(s.id, 0) - s.capacity} for s in shelters.values()]
    return sorted(rows, key=lambda r: -r["overflow"])


def analyze(*, people, helpers, shelters, ints, roads, rg, plan) -> dict:
    return {
        "slot_gap": slot_gap(people, helpers, shelters, rg),
        "spof_helpers": spof(people, helpers, plan),
        "critical_roads": critical_roads(people, shelters, ints, roads, plan),
        "shelter_overflow": shelter_overflow(shelters, plan),
    }


# Same analyses in Cypher, runnable in Aura Browser (shown on /api/gaps/cypher).
CYPHER = {
    "spof_helpers": """MATCH (m:Meta {id:1})
MATCH (h:Helper)-[:ASSIGNED {plan_version:m.current_version, role:'primary'}]->(p:Person)
WHERE NOT EXISTS { MATCH (:Helper)-[:ASSIGNED {plan_version:m.current_version, role:'backup'}]->(p) }
RETURN h.id AS helper, h.role AS role, collect(p.id) AS stranded_if_lost
ORDER BY size(stranded_if_lost) DESC LIMIT 10""",
    "slot_gap_people": """MATCH (p:Person)
WHERE NOT EXISTS {
  MATCH (h:Helper) WHERE h.available AND NOT h.injured AND 'weekday_day' IN h.availability
    AND (NOT p.mobility IN ['wheelchair','bedridden'] OR ANY(s IN ['carry','wheelchair_assist'] WHERE s IN h.skills))
    AND point.distance(point({latitude:p.lat, longitude:p.lon}), point({latitude:h.lat, longitude:h.lon})) < 400 }
RETURN p.id AS person, p.block AS block, p.priority AS priority ORDER BY priority DESC LIMIT 20""",
    "shelter_overflow": """MATCH (m:Meta {id:1})
MATCH (s:Shelter)
OPTIONAL MATCH (p:Person)-[e:EVACUATES_TO {plan_version:m.current_version}]->(s)
WITH s, count(p) AS inbound
RETURN s.name AS shelter, s.capacity AS capacity, s.occupancy AS occupancy, inbound,
       s.occupancy + inbound - s.capacity AS overflow ORDER BY overflow DESC""",
    "plan_history": """MATCH path = (latest:Plan {status:'active'})-[:SUPERSEDES*]->(old:Plan)
RETURN latest.person_id AS person, [n IN nodes(path) | n.version + ':' + n.status] AS history
ORDER BY length(path) DESC LIMIT 10""",
}
