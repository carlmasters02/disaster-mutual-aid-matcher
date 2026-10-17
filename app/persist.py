"""Neo4j persistence — the system of record, written in the background.

Every write is a batched UNWIND, serialized through one queue so ordering is
preserved, and never blocks the live demo. If Aura is not configured or
unreachable, every call is a no-op.
"""
import asyncio
import logging

from . import db
from .models import AssignmentPlan, Helper, Intersection, Person, Plan, Road, Shelter

log = logging.getLogger("tsunagu.persist")
_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()


def submit(fn, *args) -> None:
    """Fire-and-forget, ordered by the lock."""
    if not db.db_ok():
        return

    async def run():
        async with _lock:
            try:
                await fn(*args)
            except Exception as e:  # never crash the demo on a DB hiccup
                log.warning("persist %s failed: %s", fn.__name__, e)

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _row(m, drop=("nearest_intersection",)) -> dict:
    return {k: v for k, v in m.model_dump().items() if k not in drop}


async def write_dataset(ints: list[Intersection], roads: list[Road], people: list[Person],
                        helpers: list[Helper], shelters: list[Shelter]) -> None:
    await db.run_write("MATCH (n) DETACH DELETE n")
    await db.run_write("MERGE (m:Meta {id:1}) SET m.current_version = 0")
    await db.batch_write("UNWIND $rows AS r CREATE (i:Intersection) SET i = r", [_row(i) for i in ints])
    await db.batch_write(
        "UNWIND $rows AS r MATCH (a:Intersection {id:r.from_id}), (b:Intersection {id:r.to_id}) "
        "CREATE (a)-[x:ROAD]->(b) SET x = r, x.minutes_eff = r.minutes",
        [_row(r, drop=("from_id", "to_id")) | {"from_id": r.from_id, "to_id": r.to_id} for r in roads])
    for label, items in (("Person", people), ("Helper", helpers), ("Shelter", shelters)):
        await db.batch_write(f"UNWIND $rows AS r CREATE (n:{label}) SET n = r", [_row(x) for x in items])
        await db.batch_write(
            f"UNWIND $rows AS r MATCH (n:{label} {{id:r.id}}), (i:Intersection {{id:r.iid}}) CREATE (n)-[:NEAREST]->(i)",
            [{"id": x.id, "iid": x.nearest_intersection} for x in items])


async def add_person(p: Person) -> None:
    await db.run_write(
        "CREATE (n:Person) SET n = $row WITH n MATCH (i:Intersection {id:$iid}) CREATE (n)-[:NEAREST]->(i)",
        row=_row(p), iid=p.nearest_intersection)


async def write_plan(plan: AssignmentPlan, new_plans: list[Plan], superseded: dict[str, str]) -> None:
    """Assignments + evacuation routes for this version, plus Plan nodes chained by SUPERSEDES."""
    await db.run_write("MATCH (m:Meta {id:1}) SET m.current_version = $v", v=plan.version)
    await db.batch_write(
        "UNWIND $rows AS a MATCH (h:Helper {id:a.helper_id}), (p:Person {id:a.person_id}) "
        "CREATE (h)-[:ASSIGNED {role:a.role, eta_min:a.eta_min, plan_version:a.plan_version}]->(p)",
        [a.model_dump() for a in plan.assignments])
    await db.batch_write(
        "UNWIND $rows AS e MATCH (p:Person {id:e.person_id}), (s:Shelter {id:e.shelter_id}) "
        "CREATE (p)-[:EVACUATES_TO {route_ids:e.route_ids, eta_min:e.eta_min, plan_version:e.plan_version}]->(s)",
        [e.model_dump() for e in plan.evacuations])
    rows = [{"id": pl.id, "person_id": pl.person_id, "version": pl.version, "status": pl.status,
             "old": superseded.get(pl.person_id)} for pl in new_plans]
    await db.batch_write(
        "UNWIND $rows AS r MATCH (p:Person {id:r.person_id}) "
        "CREATE (n:Plan {id:r.id, person_id:r.person_id, version:r.version, status:r.status, created_at:datetime()}) "
        "CREATE (n)-[:FOR]->(p) "
        "WITH n, r OPTIONAL MATCH (old:Plan {id:r.old}) "
        "FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END | "
        "  SET old.status = 'broken' CREATE (n)-[:SUPERSEDES]->(old))", rows)


async def write_plan_docs(plan_id: str, doc_ja: str, doc_res: str) -> None:
    await db.run_write("MATCH (n:Plan {id:$id}) SET n.doc_ja = $ja, n.doc_resident_lang = $res",
                       id=plan_id, ja=doc_ja, res=doc_res)


async def write_wave(blocked: list[str], fires_on: list[str], fires_out: list[str],
                     injured: list[str], damaged: list[str], occupancy: dict[str, int]) -> None:
    await db.run_write(
        "UNWIND $ids AS b MATCH ()-[r:ROAD {id:b}]->() SET r.blocked = true, r.minutes_eff = 1e9", ids=blocked + fires_out)
    await db.run_write(
        "UNWIND $ids AS f MATCH ()-[r:ROAD {id:f}]->() SET r.on_fire = true, r.minutes_eff = 1e9", ids=fires_on)
    await db.run_write("UNWIND $ids AS f MATCH ()-[r:ROAD {id:f}]->() SET r.on_fire = false", ids=fires_out)
    await db.run_write("UNWIND $ids AS h MATCH (x:Helper {id:h}) SET x.injured = true, x.available = false", ids=injured)
    await db.run_write("UNWIND $ids AS s MATCH (x:Shelter {id:s}) SET x.damaged = true", ids=damaged)
    await db.run_write("UNWIND $rows AS r MATCH (x:Shelter {id:r.id}) SET x.occupancy = r.occ",
                       rows=[{"id": k, "occ": v} for k, v in occupancy.items()])
