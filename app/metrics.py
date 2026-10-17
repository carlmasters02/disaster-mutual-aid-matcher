"""Coverage metrics + per-person status, computed from an AssignmentPlan."""
from .models import AssignmentPlan, Coverage, Person, Shelter


def person_statuses(people: dict[str, Person], plan: AssignmentPlan | None,
                    escalated: set[str] = frozenset()) -> dict[str, str]:
    """covered | partial | uncovered | escalated | unmatched."""
    if plan is None:
        return {pid: "unmatched" for pid in people}
    prim: dict[str, int] = {}
    bak: dict[str, int] = {}
    for a in plan.assignments:
        d = prim if a.role == "primary" else bak
        d[a.person_id] = d.get(a.person_id, 0) + 1
    evac = {e.person_id for e in plan.evacuations}
    out = {}
    for pid, p in people.items():
        if pid in escalated:
            out[pid] = "escalated"
        elif prim.get(pid, 0) == 0 or pid not in evac:
            out[pid] = "uncovered"
        elif prim[pid] >= p.helpers_required and bak.get(pid, 0) >= 1:
            out[pid] = "covered"
        else:
            out[pid] = "partial"
    return out


def compute_coverage(people: dict[str, Person], shelters: dict[str, Shelter],
                     plan: AssignmentPlan) -> Coverage:
    prim: dict[str, int] = {}
    bak: dict[str, int] = {}
    etas = []
    for a in plan.assignments:
        d = prim if a.role == "primary" else bak
        d[a.person_id] = d.get(a.person_id, 0) + 1
        if a.role == "primary":
            etas.append(a.eta_min)
    evac = {e.person_id for e in plan.evacuations}
    inbound: dict[str, int] = {}
    for e in plan.evacuations:
        inbound[e.shelter_id] = inbound.get(e.shelter_id, 0) + 1
    cov = Coverage(slot=plan.time_slot, total=len(people))
    for pid, p in people.items():
        full_prim = prim.get(pid, 0) >= p.helpers_required and pid in evac
        if full_prim:
            cov.covered += 1
            if bak.get(pid, 0) >= 1:
                cov.fully_covered += 1
            else:
                cov.primary_only += 1
        if prim.get(pid, 0) == 0 or pid not in evac:
            cov.uncovered += 1
            cov.uncovered_by_priority[p.priority] = cov.uncovered_by_priority.get(p.priority, 0) + 1
    n = max(1, cov.total)
    cov.pct_covered = round(100.0 * cov.covered / n, 1)
    cov.pct_fully = round(100.0 * cov.fully_covered / n, 1)
    cov.avg_eta_min = round(sum(etas) / len(etas), 1) if etas else 0.0
    cov.shelter_loads = [
        {"id": s.id, "name": s.name, "kind": s.kind, "capacity": s.capacity,
         "occupancy": s.occupancy, "inbound": inbound.get(s.id, 0), "damaged": s.damaged}
        for s in shelters.values()
    ]
    return cov
