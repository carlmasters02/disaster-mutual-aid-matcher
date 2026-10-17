"""Runtime state + orchestration. Single writer on the asyncio loop.

The in-memory world is generated deterministically from the seed and is the
live engine; Neo4j receives every change in the background (persist.py).
"""
import asyncio
import copy
from datetime import datetime, timezone
from typing import Any

from . import db, persist
from .generator import generate
from .matcher import match_all
from .metrics import compute_coverage, person_statuses
from .models import AssignmentPlan, Coverage, Helper, Intersection, Person, Plan, Road, Shelter, TimeSlot
from .quake import QuakeParams, WaveEffect, apply_effects, plan_waves
from .routing import RoadGraph

SLOT_LABEL = {"weekday_night": "02:00", "weekday_day": "14:00"}


class QuakeRun:
    def __init__(self, params: QuakeParams, waves: list[WaveEffect], slot: TimeSlot, pre: dict, baseline: float):
        self.params = params
        self.waves = waves
        self.slot = slot
        self.pre = pre  # deep copies of the pre-quake world
        self.idx = 0
        self.series: list[float] = [baseline]
        self.compare_series: list[float] | None = None
        self.stats = {"blocked": 0, "fires": 0, "injured": 0, "damaged": 0}

    @property
    def done(self) -> bool:
        return self.idx >= len(self.waves)


class GameState:
    def __init__(self) -> None:
        self.seed = 42
        self.people: dict[str, Person] = {}
        self.helpers: dict[str, Helper] = {}
        self.shelters: dict[str, Shelter] = {}
        self.intersections: dict[str, Intersection] = {}
        self.roads: dict[str, Road] = {}
        self.rg: RoadGraph | None = None
        self.slot: TimeSlot = "weekday_night"
        self.version = 0
        self.plan: AssignmentPlan | None = None
        self.coverage: Coverage | None = None
        self.plans: dict[str, list[Plan]] = {}
        self.escalated: dict[str, dict] = {}
        self.dispatches: list[dict] = []
        self.quake: QuakeRun | None = None
        self.power_out = False
        self.report: dict | None = None
        self.lock = asyncio.Lock()
        self.apoc_available = False
        self.apoc_parity: dict = {}
        self.models_up: dict[str, bool] = {}
        self.strict_json = False
        self.matcher_engine = "greedy-priority"
        self._signature: dict[str, tuple] = {}

    # ----- world lifecycle --------------------------------------------
    def reset(self, seed: int) -> None:
        self.seed = seed
        ds = generate(seed)
        self.intersections = {i.id: i for i in ds.intersections}
        self.roads = {r.id: r for r in ds.roads}
        self.people = {p.id: p for p in ds.people}
        self.helpers = {h.id: h for h in ds.helpers}
        self.shelters = {s.id: s for s in ds.shelters}
        self.rg = RoadGraph(self.intersections, self.roads)
        self.slot = "weekday_night"
        self.version = 0
        self.quake = None
        self.power_out = False
        self.report = None
        self.escalated.clear()
        self.dispatches.clear()
        persist.submit(persist.write_dataset, ds.intersections, ds.roads, ds.people, ds.helpers, ds.shelters)
        self.rematch(baseline=True)

    def world_copy(self) -> dict:
        return {"people": copy.deepcopy(self.people), "helpers": copy.deepcopy(self.helpers),
                "shelters": copy.deepcopy(self.shelters), "roads": copy.deepcopy(self.roads)}

    def restore(self, w: dict) -> None:
        self.people, self.helpers = copy.deepcopy(w["people"]), copy.deepcopy(w["helpers"])
        self.shelters, self.roads = copy.deepcopy(w["shelters"]), copy.deepcopy(w["roads"])
        self.rg = RoadGraph(self.intersections, self.roads)
        self.power_out = False
        self.escalated.clear()

    # ----- matching + versioning ---------------------------------------
    def _sig(self, plan: AssignmentPlan) -> dict[str, tuple]:
        sig: dict[str, list] = {pid: [[], None, None] for pid in self.people}
        for a in plan.assignments:
            if a.role == "primary":
                sig[a.person_id][0].append(a.helper_id)
            else:
                sig[a.person_id][1] = a.helper_id
        for e in plan.evacuations:
            sig[e.person_id][2] = (e.shelter_id, tuple(e.route_ids))
        return {pid: (tuple(sorted(s[0])), s[1], s[2]) for pid, s in sig.items()}

    def rematch(self, *, baseline: bool = False) -> list[str]:
        """Re-run the solver. Returns ids of people whose plan changed."""
        self.version += 1
        plan = match_all(people=self.people, helpers=self.helpers, shelters=self.shelters,
                         rg=self.rg, slot=self.slot, version=self.version)
        sig = self._sig(plan)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if baseline:
            self.plans = {}
            changed = list(self.people)
        else:
            changed = [pid for pid in self.people if sig.get(pid) != self._signature.get(pid)]
        new_plans, superseded = [], {}
        for pid in changed:
            hist = self.plans.setdefault(pid, [])
            if hist and hist[-1].status == "active":
                hist[-1].status = "broken"
                superseded[pid] = hist[-1].id
            pl = Plan(id=f"PL-{pid}-v{self.version}", person_id=pid, version=self.version,
                      status="active", created_at=now, doc_language=self.people[pid].language)
            hist.append(pl)
            new_plans.append(pl)
        self._signature = sig
        self.plan = plan
        self.coverage = compute_coverage(self.people, self.shelters, plan)
        persist.submit(persist.write_plan, plan, new_plans, superseded)
        return changed

    def statuses(self) -> dict[str, str]:
        return person_statuses(self.people, self.plan, set(self.escalated))

    # ----- quake ---------------------------------------------------------
    def start_quake(self, params: QuakeParams) -> QuakeRun:
        if self.quake is not None:
            self.restore(self.quake.pre)
            self.rematch()
        waves = plan_waves(params, self.intersections, self.roads, self.helpers, self.shelters, self.seed)
        self.quake = QuakeRun(params, waves, self.slot, self.world_copy(), self.coverage.pct_covered)
        self.report = None
        return self.quake

    def step_quake(self) -> tuple[WaveEffect, list[str]]:
        q = self.quake
        w = q.waves[q.idx]
        q.idx += 1
        apply_effects(w, self.roads, self.helpers, self.shelters)
        if w.power_out:
            self.power_out = True
        q.stats["blocked"] += len(w.blocked_roads) // 2
        q.stats["fires"] += len(w.fires_new) // 2
        q.stats["injured"] += len(w.injured_helpers)
        q.stats["damaged"] += len(w.damaged_shelters)
        self.rg.rebuild(self.roads)
        changed = self.rematch()
        q.series.append(self.coverage.pct_covered)
        persist.submit(persist.write_wave, w.blocked_roads, w.fires_new, w.fires_out, w.injured_helpers,
                       w.damaged_shelters, {s.id: s.occupancy for s in self.shelters.values()})
        return w, changed

    def simulate_series(self, slot: TimeSlot) -> list[float]:
        """Headless replay of the current quake for another time slot (no LLM)."""
        q = self.quake
        w0 = copy.deepcopy(q.pre)
        rg = RoadGraph(self.intersections, w0["roads"])
        cov = lambda plan: compute_coverage(w0["people"], w0["shelters"], plan).pct_covered  # noqa: E731
        series = [cov(match_all(people=w0["people"], helpers=w0["helpers"], shelters=w0["shelters"],
                                rg=rg, slot=slot, version=0))]
        for w in q.waves:
            apply_effects(w, w0["roads"], w0["helpers"], w0["shelters"])
            rg.rebuild(w0["roads"])
            series.append(cov(match_all(people=w0["people"], helpers=w0["helpers"], shelters=w0["shelters"],
                                        rg=rg, slot=slot, version=0)))
        return series

    # ----- snapshot ------------------------------------------------------
    def plan_payload(self) -> dict:
        return {
            "statuses": self.statuses(),
            "assignments": [a.model_dump() for a in self.plan.assignments] if self.plan else [],
            "evacuations": [e.model_dump() for e in self.plan.evacuations] if self.plan else [],
        }

    async def build_snapshot(self) -> dict:
        q = self.quake
        st = self.statuses()
        return {
            "bbox_center": [(35.694 + 35.708) / 2, (139.797 + 139.815) / 2],
            "slot": self.slot,
            "version": self.version,
            "people": [{**p.model_dump(), "status": st.get(p.id, "unmatched")} for p in self.people.values()],
            "helpers": [h.model_dump() for h in self.helpers.values()],
            "shelters": [s.model_dump() for s in self.shelters.values()],
            "intersections": [i.model_dump() for i in self.intersections.values()],
            "roads": [r.model_dump() for r in self.roads.values() if not r.id.endswith("r")],
            **{k: v for k, v in self.plan_payload().items() if k != "statuses"},
            "coverage": self.coverage.model_dump() if self.coverage else None,
            "dispatches": self.dispatches[-40:],
            "escalations": list(self.escalated.values()),
            "power_out": self.power_out,
            "quake": None if q is None else {
                "params": q.params.model_dump(), "idx": q.idx, "total": len(q.waves), "slot": q.slot,
                "series": q.series, "compare_series": q.compare_series, "done": q.done,
            },
            "capabilities": {
                "neo4j": db.db_ok(), "apoc": self.apoc_available, "apoc_parity": self.apoc_parity,
                "models": self.models_up, "strict_json": self.strict_json,
                "matcher_engine": self.matcher_engine,
            },
        }


state = GameState()
