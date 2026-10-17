"""Async Neo4j driver singleton + query helpers. All Cypher funnels through here.

Degrades gracefully: if credentials are missing or Aura is unreachable,
`db_ok()` is False and callers can decide to skip persistence (pure-Python
parts of the app — generator, routing, matcher — never import this module).
"""
from neo4j import AsyncDriver, AsyncGraphDatabase

from .config import get_settings

_driver: AsyncDriver | None = None
_db_ok: bool = False


async def init_driver() -> bool:
    """Create the driver and verify connectivity. Returns True on success."""
    global _driver, _db_ok
    s = get_settings()
    if not s.neo4j_configured:
        _db_ok = False
        return False
    try:
        _driver = AsyncGraphDatabase.driver(
            s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password),
            max_connection_pool_size=10,
        )
        await _driver.verify_connectivity()
        _db_ok = True
    except Exception:
        _driver = None
        _db_ok = False
    return _db_ok


def db_ok() -> bool:
    return _db_ok


def get_driver() -> AsyncDriver:
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialised (check .env NEO4J_*)")
    return _driver


async def ping() -> bool:
    if not _db_ok or _driver is None:
        return False
    try:
        async with _driver.session() as sess:
            res = await sess.run("RETURN 1 AS ok")
            await res.single()
        return True
    except Exception:
        return False


async def run_query(cypher: str, **params) -> list[dict]:
    """Read query -> list of record dicts."""
    async with get_driver().session() as sess:
        res = await sess.run(cypher, **params)
        return [dict(r) async for r in res]


async def run_write(cypher: str, **params) -> dict:
    """Write query -> summary counters as a dict."""
    async with get_driver().session() as sess:
        res = await sess.run(cypher, **params)
        summary = await res.consume()
        c = summary.counters
        names = ("nodes_created", "nodes_deleted", "relationships_created", "relationships_deleted",
                 "properties_set", "labels_added", "constraints_added", "indexes_added")
        return {n: getattr(c, n, 0) for n in names}


async def batch_write(cypher: str, rows: list[dict], batch_size: int = 500) -> dict:
    """Run an UNWIND $rows write in batches to stay Aura-Free friendly."""
    totals: dict[str, int] = {}
    for i in range(0, len(rows), batch_size):
        counters = await run_write(cypher, rows=rows[i : i + batch_size])
        for k, v in counters.items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v
    return totals


async def close_driver() -> None:
    global _driver, _db_ok
    if _driver is not None:
        await _driver.close()
    _driver = None
    _db_ok = False
