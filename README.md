# つなぐ Tsunagu — Disaster Mutual-Aid Matcher

Builds, stress-tests and live re-plans individual evacuation plans (個別避難計画) for residents who need help evacuating, in a fictional Kyōjima (Sumida-ku) neighborhood. **All residents are synthetic.**

*The solver decides; the LLM explains.* Assignments and routes are deterministic and auditable. ai& open models (on GPUs in Japan) handle intake parsing, plan drafting, multilingual dispatch, escalations and the after-action report. Neo4j Aura is the system of record for people → helpers → roads → shelters, including plan history (`(:Plan)-[:SUPERSEDES]->(:Plan)`).

## Run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # already done
cp .env.example .env        # fill in NEO4J_* and AIAND_* (optional, see below)
.venv/bin/python scripts/check_models.py   # confirm real model ids, then put them in .env
.venv/bin/python scripts/check_apoc.py
.venv/bin/uvicorn app.main:app --port 8010
# open http://localhost:8010   (?notiles=1 = no OSM tiles, ?sync=1 = synchronous first load)
LLM_MODE=off .venv/bin/python scripts/smoke_test.py   # headless invariants, < 1 s
```

**It degrades gracefully, so the demo never stalls:**
- **No `.env`:** fully runnable in memory, with every LLM output from deterministic multilingual templates. The UI badges these as `template`.
- **`NEO4J_*` set:** every change is mirrored to Aura in the background with batched `UNWIND` writes. `/api/gaps/cypher` runs the gap analyses as Cypher. If APOC is present, `apoc.algo.dijkstra` is parity-checked against the in-memory router.
- **`AIAND_*` set:** the LLM is used everywhere, with an 18 s timeout, one retry, JSON repair, pydantic validation and a template fallback. Responses are cached in `data/llm_cache.json`, so a rehearsed golden path replays instantly. Run the full demo once with Wi-Fi before going on stage.

## Models (ai&, verified 2026-10-17)

| Task | Model | Notes |
|---|---|---|
| Intake parsing | `moonshotai/kimi-k3` | Strict JSON, about 1–2 s |
| JA plan document, report | `deepseek-ai/deepseek-v4-pro` | Reasoning disabled via `chat_template_kwargs.thinking=false` (otherwise the content comes back empty) |
| Japanese dispatches, escalations | `deepseek-ai/deepseek-v4.1-flash` | Reasoning disabled, about 1 s |
| Resident summary, non-Japanese dispatches | `qwen/qwen3.8-27b` | Cleanest output in vi/zh/ko/ne/pt |

Kimi K3 takes about 23 s per plan document, so it is used only for the short parse task.

## Demo script (3 min)

1. **Intake:** click 例文, then *Parse & Add*. The messy Japanese note becomes structured needs (walker, 3rd floor, Vietnamese). The person is matched, and the card shows the official JA plan plus a Vietnamese summary.
2. **Time-of-day gap:** toggle 🌙 02:00 → ☀️ 14:00. Coverage drops from 98.6% to 60.0% because commuting helpers are gone. The 時間帯ギャップ tab names the fix: "京島四丁目で昼間の協力者を7名募集".
3. **Quake:** click 🎯 Demo (M7.0 at 京島三丁目), then ▶ Step (or Space / →) five times, or ⏩ Auto. Roads block, fires spread, the power goes out, and everyone is re-matched live. Dispatch messages stream in the helpers' own languages. Unreachable people escalate to official responders.
4. **Compare:** click 比較 Compare. The same quake at 02:00 vs 14:00 ends at about 90% vs about 52% coverage.
5. **Report:** click 報告 Report. The after-action report includes concrete recruiting recommendations per block. Closing line: "Sensitive welfare data never leaves Japan (ai&); every relationship is a graph (Neo4j)."

Click any person for their plan and version history (v1 broken ← v2 active). Click a critical-road row to fly the map to that road.

## Layout

| File | Role |
|---|---|
| `app/generator.py` | Seeded synthetic neighborhood: 156 intersections, 277 road segments, 70 people, 80 helpers (about 50% commuters), 7 shelters |
| `app/routing.py` | networkx road graph (live engine) + APOC parity check |
| `app/matcher.py` | Greedy priority matcher: hard constraints (time slot, lift skills, ETA ≤ 15 min, cap 2 people per helper), cost = ETA + language + preferred skills + reliability; shelter selection (generator, accessibility, welfare preference) |
| `app/quake.py` | Intensity attenuation, seeded blockages revealed outward in 5 waves, fire spread, injuries, shelter damage and arrivals |
| `app/state.py` | World state, re-match + plan versioning, quake orchestration, headless compare |
| `app/gaps.py` | Slot gap by block, single points of failure, critical roads, shelter overflow (+ Cypher equivalents) |
| `app/intake.py` `plans.py` `messaging.py` `report.py` | LLM call sites; prompts carry only pseudonymous id + block + needs |
| `app/templates.py` | ja/en/zh/vi/ko/pt/ne fallback templates |
| `app/persist.py` | Background Neo4j writes |
| `static/app.js` | Leaflet map, panels, Chart.js, WebSocket client |

The matcher is greedy because scipy has no Python 3.14 wheel.
