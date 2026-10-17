"""Confirm the ai& endpoint: list models, validate configured ids, probe strict JSON.

Usage: python scripts/check_models.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.config import get_settings
from app.llm import llm


async def main() -> int:
    s = get_settings()
    if not s.llm_configured:
        print("AIAND_* not configured in .env")
        return 1
    available = await llm.list_models()
    print(f"GET /models -> {len(available)} models")
    for m in available:
        print(f"  - {m}")
    wanted = {
        "PARSE_MODEL": s.parse_model,
        "PLAN_MODEL": s.plan_model,
        "MSG_MODEL": s.msg_model,
        "REPORT_MODEL": s.report_model,
        "RESIDENT_MODEL": s.resident_model,
    }
    ok = True
    for env_name, model_id in wanted.items():
        hit = model_id in available if available else None
        mark = "OK" if hit else ("?" if hit is None else "MISSING")
        if hit is False:
            ok = False
        print(f"{env_name:14s} {model_id:30s} {mark}")
    pings = await llm.ping_models()
    for m, up in pings.items():
        print(f"ping {m:30s} {'up' if up else 'DOWN'}")
    print(f"strict json_object mode: {'supported' if llm.strict_json_ok else 'NOT supported (will use extraction+repair)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
