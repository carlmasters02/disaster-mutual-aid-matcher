"""Headless end-to-end smoke test (LLM off, Neo4j optional).

Usage: LLM_MODE=off python scripts/smoke_test.py
Asserts the demo invariants: day/night coverage gap, quake waves complete,
plan versions chain, intake parses, gaps + report render.
"""
import os
import sys
import time

sys.path.insert(0, ".")
os.environ.setdefault("LLM_MODE", "off")

from fastapi.testclient import TestClient  # noqa: E402

from app.generator import CENTER  # noqa: E402
from app.main import app  # noqa: E402


def main() -> int:
    with TestClient(app) as c:
        t0 = time.time()
        night = c.post("/api/timeslot", json={"slot": "weekday_night"}).json()["coverage"]["pct_covered"]
        day = c.post("/api/timeslot", json={"slot": "weekday_day"}).json()["coverage"]["pct_covered"]
        print(f"coverage 02:00={night}%  14:00={day}%")
        assert night >= 85 and day <= 65 and night - day >= 25, "slot gap not dramatic enough"

        r = c.post("/api/intake", json={"text": "母は82歳、歩行器を使用、3階に一人暮らし、エレベーターなし。ベトナム語しか話せません。"}).json()
        print("intake:", r["parsed"], "status:", r["status"])
        assert r["parsed"]["mobility"] == "walker" and r["parsed"]["language"] == "vi" and r["parsed"]["floor"] == 3
        assert r["plans"][0]["doc_ja"] and r["plans"][0]["doc_resident_lang"]

        c.post("/api/timeslot", json={"slot": "weekday_night"})
        c.post("/api/quake", json={"magnitude": 7.0, "epicenter_lat": CENTER[0], "epicenter_lon": CENTER[1]})
        t1 = time.time()
        res = None
        for _ in range(5):
            res = c.post("/api/quake/step").json()
        print(f"quake 02:00 series={res['series']}  ({time.time() - t1:.2f}s for 5 waves)")
        cmp_ = c.post("/api/quake/compare").json()
        print(f"quake 14:00 series={cmp_['series']}")
        assert res["done"] and res["series"][-1] > cmp_["series"][-1]

        snap = c.get("/api/snapshot").json()
        pid = next(p["id"] for p in snap["people"] if p["status"] != "covered")
        card = c.get(f"/api/person/{pid}").json()
        print(f"{pid}: {len(card['plans'])} plan versions, status={card['status']}")

        g = c.get("/api/gaps").json()
        print("gaps:", {k: len(v) if isinstance(v, list) else len(v["people"]) for k, v in g.items()})
        rep = c.post("/api/report").json()
        print("report:", rep["report"]["title"], "| recs:", rep["report"]["recommendations"][:2])
        print(f"OK in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
