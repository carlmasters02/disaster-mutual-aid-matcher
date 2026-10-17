"""Idempotent schema DDL: uniqueness constraints + useful indexes."""
from . import db

STATEMENTS: list[str] = [
    "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT helper_id IF NOT EXISTS FOR (h:Helper) REQUIRE h.id IS UNIQUE",
    "CREATE CONSTRAINT shelter_id IF NOT EXISTS FOR (s:Shelter) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT intersection_id IF NOT EXISTS FOR (i:Intersection) REQUIRE i.id IS UNIQUE",
    "CREATE CONSTRAINT plan_id IF NOT EXISTS FOR (p:Plan) REQUIRE p.id IS UNIQUE",
    "CREATE INDEX road_blocked IF NOT EXISTS FOR ()-[r:ROAD]-() ON (r.blocked)",
]


async def ensure_schema() -> None:
    if not db.db_ok():
        return
    for stmt in STATEMENTS:
        await db.run_write(stmt)
    # Meta singleton tracks the global plan version.
    await db.run_write(
        "MERGE (m:Meta {id: 1}) ON CREATE SET m.current_version = 0"
    )
