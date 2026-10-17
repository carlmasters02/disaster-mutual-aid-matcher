"""Check whether apoc.algo.dijkstra exists on this Aura instance.

Usage: python scripts/check_apoc.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app import db


async def main() -> int:
    if not await db.init_driver():
        print("Neo4j not reachable (check NEO4J_* in .env)")
        return 1
    found = False
    for q in (
        "SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'apoc' RETURN name",
        "CALL dbms.procedures() YIELD name WHERE name STARTS WITH 'apoc' RETURN name",
    ):
        try:
            rows = await db.run_query(q)
            names = [r["name"] for r in rows]
            if names:
                found = True
                print(f"APOC procedures visible ({len(names)}):")
                for n in sorted(names):
                    print(f"  - {n}")
                print(
                    "apoc.algo.dijkstra:",
                    "AVAILABLE" if "apoc.algo.dijkstra" in names else "MISSING",
                )
                break
        except Exception as e:
            print(f"probe failed ({e.__class__.__name__}), trying next form")
    if not found:
        print("No APOC procedures visible. Live routing uses the in-memory "
              "networkx engine anyway (by design); APOC parity check will be skipped.")
    await db.close_driver()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
