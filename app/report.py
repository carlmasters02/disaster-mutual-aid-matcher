"""After-action report (REPORT_MODEL) with a numbers-filled template fallback."""
from pydantic import BaseModel, Field

from .config import get_settings
from .llm import llm

SYSTEM = ("あなたは自治体の防災アナリストです。地震シミュレーションの結果から振り返り報告を日本語で作成します。"
          "与えられた数値のみを使い、年度や日付を創作しないこと。昼間に協力者が不足する主因は通勤による不在であり、"
          "提言には daytime_helpers_to_recruit に基づく『◯丁目で平日昼間の協力者を◯名募集』形式の具体策を必ず含めてください。"
          '出力はJSONのみ: {"title": str, "sections": [{"heading": str, "body": str}], "recommendations": [str]}')


class Section(BaseModel):
    heading: str
    body: str


class Report(BaseModel):
    title: str
    sections: list[Section] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


def template_report(facts: dict) -> Report:
    q = facts.get("quake") or {}
    sg = facts["slot_gap"]
    secs = [
        Section(heading="カバー率の比較 / Coverage",
                body=(f"平常時: 夜間02:00 {sg['night_pct']}% ／ 平日昼14:00 {sg['day_pct']}%。"
                      + (f" 地震後({q.get('slot_label', '')}): {q['series'][0]}% → {q['series'][-1]}%。" if q.get("series") else "")
                      + (f" 比較時間帯: {q['compare_series'][0]}% → {q['compare_series'][-1]}%。" if q.get("compare_series") else ""))),
        Section(heading="被害の概要 / Damage",
                body=(f"M{q.get('magnitude', '-')}、閉塞道路{q.get('blocked', 0)}区間、延焼{q.get('fires', 0)}区間、"
                      f"負傷協力者{q.get('injured', 0)}名、被災避難所{q.get('damaged', 0)}か所、"
                      f"公的機関へのエスカレーション{q.get('escalations', 0)}件。") if q else "地震シミュレーション未実施。"),
        Section(heading="重要道路 / Critical roads",
                body="、".join(f"{r['road_id']}（孤立{len(r['strands'])}名・遅延{len(r['delays'])}名）"
                              for r in facts["critical_roads"][:5]) or "該当なし"),
        Section(heading="単一障害点 / Single points of failure",
                body=f"予備のいない要支援者を抱える協力者: {len(facts['spof_helpers'])}名。"),
    ]
    recs = [f"{b['block']}で平日昼間の協力者を{b['recruit']}名募集（商店主・在宅の退職者・在宅勤務者）"
            for b in sg["blocks"][:3] if b["recruit"] > 0]
    if facts["critical_roads"]:
        r = facts["critical_roads"][0]
        recs.append(f"道路{r['road_id']}（幅{r['width_m']}m）のブロック塀改修・代替経路の確保")
    over = [s for s in facts["shelter_overflow"] if s["overflow"] > 0]
    if over:
        recs.append(f"{over[0]['name']}の収容超過({over[0]['overflow']}名)に備え、近隣施設と受入協定を締結")
    recs.append("予備協力者のいない要支援者について、個別避難計画の見直し会議を開催")
    return Report(title="振り返り報告 After-Action Report", sections=secs, recommendations=recs)


async def build_report(facts: dict) -> tuple[Report, bool]:
    s = get_settings()
    slim = {
        "coverage_no_quake": {"weekday_02:00_pct": facts["slot_gap"]["night_pct"],
                              "weekday_14:00_pct": facts["slot_gap"]["day_pct"]},
        "daytime_helper_shortage_by_block": [
            {"block": b["block"], "residents_without_daytime_helpers": b["people"],
             "missing_helper_assignments": b["missing_slots"], "daytime_helpers_to_recruit": b["recruit"]}
            for b in facts["slot_gap"]["blocks"]],
        "quake": facts.get("quake"),
        "critical_roads": [{k: r[k] for k in ("road_id", "width_m", "users")} | {"strands": len(r["strands"])}
                           for r in facts["critical_roads"][:5]],
        "spof_helpers": len(facts["spof_helpers"]),
        "shelter_overflow": [s for s in facts["shelter_overflow"] if s["overflow"] > -20][:4],
    }
    obj, fb = await llm.complete_json(model=s.report_model, system=SYSTEM, user=f"データ: {slim}",
                                      schema=Report, max_tokens=1400)
    if fb or obj is None:
        return template_report(facts), True
    return obj, False  # type: ignore[return-value]
