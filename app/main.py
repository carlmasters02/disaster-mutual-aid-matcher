"""FastAPI entrypoint: static UI, REST API, WebSocket, lifespan wiring."""
import asyncio
import logging
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, gaps, persist, schema
from .config import get_settings
from .events import (
    DispatchEvent, EscalationEvent, PatchEvent, RematchEvent, ReportEvent, SnapshotEvent, WaveEvent, manager,
)
from .generator import block_of, nearest_intersection
from .intake import parse_note
from .llm import llm
from .messaging import dispatch_message, explain_escalation
from .models import Person
from .plans import draft_plan_docs
from .quake import QuakeParams
from .report import build_report
from .routing import verify_apoc_parity
from .state import SLOT_LABEL, state

log = logging.getLogger("tsunagu")
_keepalive_task: asyncio.Task | None = None
_bg: set[asyncio.Task] = set()
DISPATCH_PER_WAVE = 6
ESCALATION_LLM_PER_WAVE = 4


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _keepalive() -> None:
    """Aura Free idles out; ping every 5 min so the demo never hits a cold pool."""
    while True:
        await asyncio.sleep(300)
        await db.ping()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keepalive_task
    s = get_settings()
    if await db.init_driver():
        try:
            await schema.ensure_schema()
        except Exception as e:  # never block startup on DDL
            log.warning("schema setup failed: %s", e)
        state.apoc_available = await _check_apoc()
        _keepalive_task = asyncio.create_task(_keepalive())
    state.reset(s.scenario_seed)
    if state.apoc_available:
        async def parity():
            await asyncio.sleep(8)  # let the dataset write land first
            state.apoc_parity = await verify_apoc_parity(state.rg)
        spawn(parity())
    if s.llm_mode == "live" and s.llm_configured:
        state.models_up = await llm.ping_models()
        state.strict_json = llm.strict_json_ok
    manager.set_snapshot_provider(state.build_snapshot)
    yield
    if _keepalive_task:
        _keepalive_task.cancel()
    await db.close_driver()


async def _check_apoc() -> bool:
    for q in ("SHOW PROCEDURES YIELD name WHERE name = 'apoc.algo.dijkstra' RETURN name",
              "CALL dbms.procedures() YIELD name WHERE name = 'apoc.algo.dijkstra' RETURN name"):
        try:
            return len(await db.run_query(q)) > 0
        except Exception:
            continue
    return False


app = FastAPI(title="Tsunagu (つなぐ)", lifespan=lifespan)


def rematch_event(plans_changed: int, wave: int | None = None) -> RematchEvent:
    return RematchEvent(plans_changed=plans_changed, coverage=state.coverage, wave=wave,
                        slot=state.slot, **state.plan_payload())


# ---------------------------------------------------------------- basics
@app.get("/api/health")
async def health() -> dict:
    return {
        "neo4j": await db.ping(), "apoc": state.apoc_available, "apoc_parity": state.apoc_parity,
        "models": state.models_up, "strict_json": state.strict_json, "llm_mode": get_settings().llm_mode,
        "matcher_engine": state.matcher_engine,
        "counts": {k: len(getattr(state, k)) for k in ("people", "helpers", "shelters", "intersections", "roads")},
        "slot": state.slot, "version": state.version,
    }


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return await state.build_snapshot()


class ResetReq(BaseModel):
    seed: int | None = None


@app.post("/api/reset")
async def reset(req: ResetReq) -> dict:
    async with state.lock:
        state.reset(req.seed if req.seed is not None else get_settings().scenario_seed)
    await manager.broadcast(SnapshotEvent(payload=await state.build_snapshot()))
    return {"ok": True, "coverage": state.coverage}


class SlotReq(BaseModel):
    slot: str


@app.post("/api/timeslot")
async def timeslot(req: SlotReq) -> dict:
    if req.slot not in SLOT_LABEL:
        raise HTTPException(400, "slot must be weekday_night or weekday_day")
    async with state.lock:
        had_quake = state.quake is not None
        if had_quake:
            state.restore(state.quake.pre)
            state.quake = None
        state.slot = req.slot  # type: ignore[assignment]
        state.rematch(baseline=True)
    if had_quake:
        await manager.broadcast(SnapshotEvent(payload=await state.build_snapshot()))
    else:
        await manager.broadcast(rematch_event(len(state.people)))
    return {"slot": state.slot, "coverage": state.coverage}


# ---------------------------------------------------------------- person card
def _person_ctx(pid: str):
    p = state.people[pid]
    prim, backup = [], None
    for a in state.plan.assignments if state.plan else []:
        if a.person_id != pid:
            continue
        if a.role == "primary":
            prim.append((state.helpers[a.helper_id], a.eta_min))
        else:
            backup = (state.helpers[a.helper_id], a.eta_min)
    ev = next((e for e in state.plan.evacuations if e.person_id == pid), None) if state.plan else None
    shelter = state.shelters[ev.shelter_id] if ev else None
    return p, prim, backup, ev, shelter


async def ensure_docs(pid: str) -> None:
    hist = state.plans.get(pid)
    if not hist or hist[-1].doc_ja:
        return
    cur = hist[-1]
    p, prim, backup, ev, shelter = _person_ctx(pid)
    docs = await draft_plan_docs(p, prim, backup, shelter, ev.eta_min if ev else None, cur.version)
    cur.doc_ja, cur.doc_resident_lang, cur.doc_language, cur.fallback = (
        docs.doc_ja, docs.doc_resident_lang, docs.doc_language, docs.fallback)
    persist.submit(persist.write_plan_docs, cur.id, cur.doc_ja, cur.doc_resident_lang)


@app.get("/api/person/{pid}")
async def person(pid: str) -> dict:
    if pid not in state.people:
        raise HTTPException(404)
    await ensure_docs(pid)
    p, prim, backup, ev, shelter = _person_ctx(pid)
    helper = lambda h, eta, role: {"id": h.id, "name": h.display_name, "role": h.role, "languages": h.languages,  # noqa: E731
                                   "skills": h.skills, "eta_min": eta, "as": role, "lat": h.lat, "lon": h.lon}
    return {
        "person": p.model_dump(), "status": state.statuses().get(pid),
        "helpers": [helper(h, e, "primary") for h, e in prim] + ([helper(*backup, "backup")] if backup else []),
        "evacuation": ev.model_dump() | {"shelter_name": shelter.name, "shelter_kind": shelter.kind} if ev else None,
        "plans": [pl.model_dump() for pl in reversed(state.plans.get(pid, []))],
        "escalation": state.escalated.get(pid),
        "uncovered": next((u.model_dump() for u in state.plan.uncovered if u.person_id == pid), None),
    }


# ---------------------------------------------------------------- intake
class IntakeReq(BaseModel):
    text: str


@app.post("/api/intake")
async def intake(req: IntakeReq) -> dict:
    parsed, fb = await parse_note(req.text)
    async with state.lock:
        n = len(state.people) + 1
        rng = random.Random(f"{state.seed}:intake:{n}")
        it = rng.choice(sorted(state.intersections.values(), key=lambda i: i.id))
        lat, lon = it.lat + rng.uniform(-0.0002, 0.0002), it.lon + rng.uniform(-0.0002, 0.0002)
        age = parsed.age or 80
        prio = 5 if ("oxygen" in parsed.needs or parsed.mobility == "bedridden") else (
            4 if parsed.mobility == "wheelchair" or (parsed.lives_alone and age >= 85) else (
                3 if parsed.mobility == "walker" or parsed.floor >= 3 else 2))
        p = Person(id=f"P-{n:03d}", display_name=f"新規登録 {n}", age=age, lat=round(lat, 6), lon=round(lon, 6),
                   floor=parsed.floor, lives_alone=parsed.lives_alone, language=parsed.language,
                   needs=parsed.needs, mobility=parsed.mobility, helpers_required=parsed.helpers_required,
                   medical_power_dependent="oxygen" in parsed.needs, priority=prio, notes_freeform=req.text,
                   block=block_of(lon),
                   nearest_intersection=nearest_intersection(lat, lon, list(state.intersections.values())))
        state.people[p.id] = p
        persist.submit(persist.add_person, p)
        changed = state.rematch()
    await manager.broadcast(PatchEvent(payload={"kind": "person_added", "person": p.model_dump()}))
    await manager.broadcast(rematch_event(len(changed)))
    await ensure_docs(p.id)
    card = await person(p.id)
    return {"parsed": parsed.model_dump(), "parsed_fallback": fb, **card}


# ---------------------------------------------------------------- quake
class QuakeReq(BaseModel):
    magnitude: float = 7.0
    epicenter_lat: float
    epicenter_lon: float


@app.post("/api/quake")
async def quake_start(req: QuakeReq) -> dict:
    async with state.lock:
        q = state.start_quake(QuakeParams(**req.model_dump()))
    await manager.broadcast(SnapshotEvent(payload=await state.build_snapshot()))
    return {"waves": len(q.waves), "baseline": q.series[0], "slot": q.slot}


async def _wave_comms(changed: list[str], prev_assign: set[tuple[str, str]], wave: int) -> None:
    """Dispatch messages for newly assigned helpers + escalations. Runs in background."""
    new = []
    for a in state.plan.assignments:
        if a.role == "primary" and a.person_id in changed and (a.helper_id, a.person_id) not in prev_assign:
            new.append(a)
    # surface multilingual messages first, then priority
    new.sort(key=lambda a: (state.helpers[a.helper_id].languages[0] == "ja", -state.people[a.person_id].priority))
    seen_langs: set[str] = set()
    picked = []
    for a in new:
        lang = state.helpers[a.helper_id].languages[0]
        if len(picked) < DISPATCH_PER_WAVE and (lang not in seen_langs or len(picked) >= 3):
            picked.append(a)
            seen_langs.add(lang)

    async def one_dispatch(a):
        h, p = state.helpers[a.helper_id], state.people[a.person_id]
        ev = next((e for e in state.plan.evacuations if e.person_id == p.id), None)
        lang, text, fb = await dispatch_message(h, p, state.shelters[ev.shelter_id] if ev else None, a.eta_min)
        d = DispatchEvent(helper_id=h.id, helper_name=h.display_name, person_id=p.id, lang=lang, text=text, fallback=fb)
        state.dispatches.append(d.model_dump())
        await manager.broadcast(d)

    esc_people = []
    prim_count: dict[str, int] = {}
    for a in state.plan.assignments:
        if a.role == "primary":
            prim_count[a.person_id] = prim_count.get(a.person_id, 0) + 1
    for u in state.plan.uncovered:
        p = state.people[u.person_id]
        if u.person_id in state.escalated:
            continue
        if u.reason == "unreachable" or (prim_count.get(p.id, 0) == 0 and p.priority >= 4):
            esc_people.append((p, u.reason))
    esc_people.sort(key=lambda x: -x[0].priority)

    async def one_escalation(k, p, reason):
        if k < ESCALATION_LLM_PER_WAVE:
            text, res, fb = await explain_escalation(p, reason, f"wave {wave}: re-match over all available helpers")
        else:
            from .messaging import template_escalation
            text, res = template_escalation(p, reason)
            fb = True
        e = EscalationEvent(person_id=p.id, priority=p.priority, reason=reason,
                            explanation=text + ("\n必要資源: " + "、".join(res) if res else ""), fallback=fb)
        state.escalated[p.id] = e.model_dump() | {"block": p.block, "wave": wave, "lat": p.lat, "lon": p.lon}
        await manager.broadcast(e)

    # mark escalated immediately so statuses flash red without waiting for the LLM
    for p, reason in esc_people:
        state.escalated.setdefault(p.id, {"person_id": p.id, "priority": p.priority, "reason": reason,
                                          "explanation": "…", "block": p.block, "wave": wave})
    if esc_people:
        await manager.broadcast(rematch_event(0, wave))
    await asyncio.gather(*(one_dispatch(a) for a in picked),
                         *(one_escalation(k, p, r) for k, (p, r) in enumerate(esc_people)),
                         return_exceptions=True)


@app.post("/api/quake/step")
async def quake_step() -> dict:
    async with state.lock:
        q = state.quake
        if q is None:
            raise HTTPException(409, "no active quake")
        if q.done:
            return {"done": True, "wave": q.idx, "series": q.series}
        prev = {(a.helper_id, a.person_id) for a in state.plan.assignments}
        w, changed = state.step_quake()
    await manager.broadcast(WaveEvent(
        n=w.n, total_waves=len(q.waves), blocked_roads=w.blocked_roads, fires=w.fires_new,
        injured_helpers=w.injured_helpers, damaged_shelters=w.damaged_shelters,
        dropped_helpers=[]))
    await manager.broadcast(PatchEvent(payload={
        "kind": "wave_state", "fires_out": w.fires_out, "power_out": state.power_out,
        "shelters": [s.model_dump() for s in state.shelters.values()]}))
    await manager.broadcast(rematch_event(len(changed), w.n))
    spawn(_wave_comms(changed, prev, w.n))
    return {"done": q.done, "wave": w.n, "plans_changed": len(changed), "coverage": state.coverage,
            "series": q.series}


@app.post("/api/quake/compare")
async def quake_compare() -> dict:
    q = state.quake
    if q is None:
        raise HTTPException(409, "no active quake")
    other = "weekday_day" if q.slot == "weekday_night" else "weekday_night"
    q.compare_series = state.simulate_series(other)  # type: ignore[arg-type]
    await manager.broadcast(PatchEvent(payload={"kind": "compare", "slot": other, "series": q.compare_series}))
    return {"slot": other, "series": q.compare_series, "own": {"slot": q.slot, "series": q.series}}


# ---------------------------------------------------------------- gaps + report
def _gaps() -> dict:
    return gaps.analyze(people=state.people, helpers=state.helpers, shelters=state.shelters,
                        ints=state.intersections, roads=state.roads, rg=state.rg, plan=state.plan)


@app.get("/api/gaps")
async def get_gaps() -> dict:
    return await asyncio.to_thread(_gaps)


@app.get("/api/gaps/cypher")
async def gaps_cypher() -> dict:
    out = {}
    for name, q in gaps.CYPHER.items():
        if not db.db_ok():
            out[name] = {"cypher": q, "rows": None, "note": "Neo4j not configured"}
            continue
        try:
            out[name] = {"cypher": q, "rows": await db.run_query(q)}
        except Exception as e:
            out[name] = {"cypher": q, "rows": None, "error": str(e)}
    return out


@app.post("/api/report")
async def report() -> dict:
    g = await asyncio.to_thread(_gaps)
    q = state.quake
    facts = {**g}
    if q is not None:
        if q.compare_series is None:
            other = "weekday_day" if q.slot == "weekday_night" else "weekday_night"
            q.compare_series = state.simulate_series(other)  # type: ignore[arg-type]
        facts["quake"] = {"magnitude": q.params.magnitude, "slot_label": SLOT_LABEL[q.slot],
                          "series": q.series, "compare_series": q.compare_series,
                          "escalations": len(state.escalated), **q.stats}
    rep, fb = await build_report(facts)
    payload = rep.model_dump() | {"facts": {"slot_gap": g["slot_gap"]["blocks"], "quake": facts.get("quake")}}
    state.report = payload
    await manager.broadcast(ReportEvent(payload=payload, fallback=fb))
    return {"report": payload, "fallback": fb}


# ---------------------------------------------------------------- ws + static
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
