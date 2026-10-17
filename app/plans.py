"""Individual evacuation plan (個別避難計画) drafting.

The solver's output is turned into deterministic plan parts in code; the LLM
only wordsmiths them. Prompts carry a pseudonymous id + block + needs —
never names or addresses (helpers appear as 協力者A/B and are substituted
back locally after the call).
"""
from pydantic import BaseModel

from .config import get_settings
from .llm import llm
from .models import Helper, Person, Shelter
from .templates import (
    LANG_EN, NEED_EN, NEED_JA, RESIDENT, handling_for, items_for,
)

SYSTEM_JA = ("あなたは区市町村の防災担当職員です。個別避難計画書を正式な文体で作成してください。"
             "事実は与えられたデータのみ使用し、推測で項目を増やさないでください。出力はJSONのみ。")
SYSTEM_RES = ("Rewrite the evacuation plan as a warm, simple message in {lang}. Use short sentences an "
              "elderly person or their family can understand. Keep placeholders like 協力者A unchanged. "
              'Output JSON only: {{"text": "..."}}')


class DocJA(BaseModel):
    doc_ja: str


class DocText(BaseModel):
    text: str


class PlanDocs(BaseModel):
    doc_ja: str
    doc_resident_lang: str
    doc_language: str
    fallback: bool


def _parts(p: Person, prim: list[tuple[Helper, float]], backup: tuple[Helper, float] | None,
           shelter: Shelter | None, route_eta: float | None, version: int) -> dict:
    letters = "ABCDE"
    alias = {}
    prim_lines = []
    for k, (h, eta) in enumerate(prim):
        alias[f"協力者{letters[k]}"] = h.display_name
        prim_lines.append(f"協力者{letters[k]}（{h.role}、到着目安{eta:.0f}分）")
    bak_line = "未確保"
    if backup:
        alias["協力者予備"] = backup[0].display_name
        bak_line = f"協力者予備（{backup[0].role}、到着目安{backup[1]:.0f}分）"
    items_ja, items_en = items_for(p.needs)
    hand_ja, hand_en = handling_for(p.needs, p.mobility)
    needs_ja = "、".join(NEED_JA.get(n, n) for n in p.needs) or "歩行に時間がかかる"
    medical = []
    if "oxygen" in p.needs:
        medical.append("在宅酸素使用。停電時は非常用電源のある避難所へ")
    if "dialysis" in p.needs:
        medical.append("人工透析。72時間以内に透析可能な施設へ搬送調整")
    if "dementia" in p.needs:
        medical.append("認知症。顔見知りの協力者が声かけ")
    return {
        "pid": p.id, "block": p.block, "version": version, "needs_ja": needs_ja,
        "needs_en": ", ".join(NEED_EN.get(n, n) for n in p.needs) or "slow walker",
        "floor": p.floor, "lives_alone": p.lives_alone,
        "primary": "、".join(prim_lines) or "未確保", "backup": bak_line,
        "shelter": shelter.name if shelter else "未定（経路寸断）",
        "shelter_kind": shelter.kind if shelter else "",
        "route_eta": f"{route_eta:.0f}" if route_eta is not None else "—",
        "wheelchair_route": p.mobility in ("wheelchair", "bedridden"),
        "items_ja": "、".join(items_ja), "items_en": ", ".join(items_en),
        "handling_ja": hand_ja, "handling_en": hand_en,
        "medical": "；".join(medical) or "特記なし",
        "contact": "協力者（主）→ 協力者（予備）→ 地区の民生委員 → 区役所 防災課",
        "alias": alias,
        "eta_first": f"{prim[0][1]:.0f}" if prim else "—",
    }


def template_doc_ja(d: dict) -> str:
    route = f"徒歩約{d['route_eta']}分" + ("（幅員2.5m以上・ブロック塀の少ない経路）" if d["wheelchair_route"] else "")
    return (
        f"個別避難計画書（{d['pid']}／第{d['version']}版）\n地区: {d['block']}\n\n"
        f"1. 支援内容: {d['needs_ja']}。{d['floor']}階{'・一人暮らし' if d['lives_alone'] else ''}。介助: {d['handling_ja']}\n"
        f"2. 担当協力者: 主 {d['primary']}／予備 {d['backup']}\n"
        f"3. 避難経路: {route}\n"
        f"4. 避難先: {d['shelter']}{'（' + d['shelter_kind'] + '）' if d['shelter_kind'] else ''}\n"
        f"5. 持ち物: {d['items_ja']}\n"
        f"6. 医療上の注意: {d['medical']}\n"
        f"7. 連絡順序: {d['contact']}"
    )


def template_resident(p: Person, d: dict, helper_names: list[str]) -> str:
    lang = p.language if p.language in RESIDENT else "en"
    sep = "、" if lang in ("ja", "zh") else ", "
    names = sep.join(helper_names) or {"ja": "地域の協力者"}.get(lang, "a local helper")
    items = d["items_ja"] if lang == "ja" else d["items_en"]
    return RESIDENT[lang].format(name=p.display_name, helpers=names, eta=d["eta_first"],
                                 shelter=d["shelter"], items=items)


def _unalias(text: str, alias: dict[str, str]) -> str:
    for k in sorted(alias, key=len, reverse=True):
        text = text.replace(k, alias[k])
    return text


async def draft_plan_docs(p: Person, prim: list[tuple[Helper, float]], backup: tuple[Helper, float] | None,
                          shelter: Shelter | None, route_eta: float | None, version: int) -> PlanDocs:
    s = get_settings()
    d = _parts(p, prim, backup, shelter, route_eta, version)
    names = [h.display_name for h, _ in prim]
    fallback = False

    facts = {k: v for k, v in d.items() if k not in ("alias", "needs_en", "items_en", "handling_en")}
    user = ("次のデータから個別避難計画書を作成。必須の節: 支援内容／担当協力者（主・予備）／避難経路／"
            "避難先／持ち物／医療上の注意／連絡順序。協力者A等の表記はそのまま使うこと。\n"
            f"データ: {facts}\n出力: {{\"doc_ja\": \"...\"}}")
    obj, fb = await llm.complete_json(model=s.plan_model, system=SYSTEM_JA, user=user, schema=DocJA, max_tokens=900)
    if fb or obj is None:
        doc_ja_alias = template_doc_ja(d)
        fallback = True
    else:
        doc_ja_alias = obj.doc_ja  # type: ignore[attr-defined]
    doc_ja = _unalias(doc_ja_alias, d["alias"])

    lang = p.language
    lang_name = LANG_EN.get(lang, "English") if lang != "ja" else "やさしい日本語 (easy Japanese)"
    obj2, fb2 = await llm.complete_json(model=s.resident_model, system=SYSTEM_RES.format(lang=lang_name),
                                        user=doc_ja_alias, schema=DocText, max_tokens=600)
    if fb2 or obj2 is None:
        res = template_resident(p, d, names)
        fallback = True
    else:
        res = _unalias(obj2.text, d["alias"])  # type: ignore[attr-defined]
    return PlanDocs(doc_ja=doc_ja, doc_resident_lang=res, doc_language=lang, fallback=fallback)
