"""Helper dispatch messages (LINE-style, in the helper's language) and
escalation explainers for people the solver could not cover."""
from pydantic import BaseModel, Field

from .config import get_settings
from .llm import llm
from .models import Helper, Person, Shelter
from .templates import (
    DISPATCH, ESCALATION_JA, LANG_EN, NEED_EN, NEED_JA, RESOURCES, handling_for, items_for,
)

SYSTEM_MSG = ("You are a disaster HQ operator. Write a short LINE-style dispatch message to a volunteer helper. "
              "The message MUST be written entirely in {lang} (translate everything; keep place names and IDs as given). "
              "About 200 characters. Include: who to help, where to take them, what to bring, handling notes, "
              "and what to do if the road is blocked. Output JSON only: {{\"text\": \"...\"}}")
SYSTEM_ESC = ("あなたは防災本部の調整担当です。自主防災組織で支援できなかった要支援者について、"
              "消防・警察など公的機関への引き継ぎ文を日本語で簡潔に書いてください（120字程度）。"
              '出力はJSONのみ: {"explanation_ja": "...", "recommended_resources": ["..."]}')


class MsgOut(BaseModel):
    text: str


class EscOut(BaseModel):
    explanation_ja: str
    recommended_resources: list[str] = Field(default_factory=list)


def helper_lang(h: Helper) -> str:
    return h.languages[0] if h.languages else "ja"


def _ctx(p: Person, shelter: Shelter | None, eta: float, lang: str) -> dict:
    ja = lang == "ja"
    items_ja, items_en = items_for(p.needs)
    hand_ja, hand_en = handling_for(p.needs, p.mobility)
    labels = NEED_JA if ja else NEED_EN
    return {
        "pid": p.id, "block": p.block, "eta": f"{eta:.0f}",
        "needs": ("、" if ja else ", ").join(labels.get(n, n) for n in p.needs) or ("歩行介助" if ja else "walking assistance"),
        "shelter": shelter.name if shelter else ("本部の指示を待つ" if ja else "await HQ instructions"),
        "items": ("、".join(items_ja[:3]) if ja else ", ".join(items_en[:3])),
        "notes": hand_ja if ja else hand_en,
    }


async def dispatch_message(h: Helper, p: Person, shelter: Shelter | None, eta: float) -> tuple[str, str, bool]:
    """Returns (lang, text, fallback)."""
    lang = helper_lang(h)
    ctx = _ctx(p, shelter, eta, lang)
    s = get_settings()
    lang_name = LANG_EN.get(lang, lang)
    # non-Japanese messages go to the stronger multilingual model (DeepSeek-flash drifts back to Japanese)
    model = s.msg_model if lang == "ja" else s.resident_model
    obj, fb = await llm.complete_json(
        model=model, system=SYSTEM_MSG.format(lang=lang_name),
        user=f"Data: {ctx}\nWrite the message in {lang_name} only.", schema=MsgOut, max_tokens=400)
    if fb or obj is None:
        return lang, DISPATCH.get(lang, DISPATCH["en"]).format(**ctx), True
    return lang, obj.text, False  # type: ignore[attr-defined]


def template_escalation(p: Person, reason: str) -> tuple[str, list[str]]:
    res = [RESOURCES[n] for n in p.needs if n in RESOURCES] or ["救助隊1隊"]
    needs = "、".join(NEED_JA.get(n, n) for n in p.needs) or "歩行困難"
    text = f"{p.block}・{p.id}（優先度{p.priority}、{needs}、{p.floor}階）: {ESCALATION_JA.get(reason, reason)}"
    return text, res


async def explain_escalation(p: Person, reason: str, tried: str) -> tuple[str, list[str], bool]:
    s = get_settings()
    facts = {"id": p.id, "block": p.block, "priority": p.priority, "needs": p.needs,
             "mobility": p.mobility, "floor": p.floor, "reason_code": reason, "tried": tried}
    obj, fb = await llm.complete_json(model=s.msg_model, system=SYSTEM_ESC, user=f"データ: {facts}",
                                      schema=EscOut, max_tokens=350)
    if fb or obj is None:
        text, res = template_escalation(p, reason)
        return text, res, True
    return obj.explanation_ja, obj.recommended_resources, False  # type: ignore[attr-defined]
