"""Intake parser: messy free-text note (ja/en) -> structured needs.

LLM (PARSE_MODEL, strict JSON) with a keyword-rule fallback. Only the note
text is sent — no names or addresses are attached by the system.
"""
import re
import unicodedata

from pydantic import BaseModel, Field, field_validator

from .config import get_settings
from .llm import llm
from .models import LANGS, NEEDS

SYSTEM = ("あなたは避難行動要支援者の申込内容を構造化するアシスタントです。出力はJSONのみ。"
          "個人情報の推測はせず、書かれた事実だけを抽出してください。")
USER_TMPL = """次の申込メモから構造化データを抽出してください。
メモ: \"\"\"{note}\"\"\"
出力JSONスキーマ:
{{"needs": ["wheelchair|walker|bedridden|dementia|visual|hearing|oxygen|dialysis|infant|non_japanese_speaker|pet"],
 "mobility": "walk|walker|wheelchair|bedridden",
 "helpers_required": 1|2|3, "floor": int, "lives_alone": bool, "age": int|null,
 "language": "ja|en|zh|vi|ko|ne|pt", "confidence": 0.0-1.0}}
language は本人が話せる言語。不明な場合: mobility="walk", helpers_required=1, floor=1, lives_alone=false, language="ja"。"""


class IntakeResult(BaseModel):
    needs: list[str] = Field(default_factory=list)
    mobility: str = "walk"
    helpers_required: int = 1
    floor: int = 1
    lives_alone: bool = False
    age: int | None = None
    language: str = "ja"
    confidence: float = 0.5

    @field_validator("needs")
    @classmethod
    def _needs(cls, v: list[str]) -> list[str]:
        return [n for n in dict.fromkeys(v) if n in NEEDS]

    @field_validator("mobility")
    @classmethod
    def _mob(cls, v: str) -> str:
        return v if v in ("walk", "walker", "wheelchair", "bedridden") else "walk"

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        return v if v in LANGS else "ja"

    @field_validator("helpers_required")
    @classmethod
    def _req(cls, v: int) -> int:
        return max(1, min(3, int(v)))


KEYWORDS = [
    ("bedridden", r"寝たきり|bedridden|bed-bound"),
    ("wheelchair", r"車椅子|車いす|くるまいす|wheelchair"),
    ("walker", r"歩行器|シルバーカー|walker|walking frame"),
    ("oxygen", r"酸素|oxygen"),
    ("dialysis", r"透析|dialysis"),
    ("dementia", r"認知症|物忘れ|dementia|alzheimer"),
    ("hearing", r"耳が遠い|難聴|聴覚|hard of hearing|deaf"),
    ("visual", r"目が見えない|視覚|弱視|blind|low vision"),
    ("infant", r"乳児|赤ちゃん|乳幼児|infant|baby|toddler"),
    ("pet", r"犬|猫|ペット|\bdogs?\b|\bcats?\b|\bpets?\b"),
]
LANG_KW = [
    ("vi", r"ベトナム|vietnam"), ("zh", r"中国語|中文|chinese|mandarin"),
    ("ko", r"韓国|korean"), ("ne", r"ネパール|nepal"), ("pt", r"ポルトガル|ブラジル|portugu|brazil"),
    ("en", r"英語しか|英語のみ|english only|only english|speaks english"),
]


def keyword_fallback(text: str) -> IntakeResult:
    t = unicodedata.normalize("NFKC", text).lower()
    needs = [n for n, pat in KEYWORDS if re.search(pat, t)]
    mobility = next((m for m in ("bedridden", "wheelchair", "walker") if m in needs), "walk")
    floor = 1
    m = re.search(r"(\d+)\s*階", t) or re.search(r"floor\s*(\d+)", t) or re.search(r"(\d+)(?:st|nd|rd|th)\s*floor", t)
    if m:
        floor = max(1, min(30, int(m.group(1))))
    age = None
    m = re.search(r"(\d{1,3})\s*歳", t) or re.search(r"(?:is|aged?)\s*(\d{2,3})", t)
    if m:
        age = int(m.group(1))
    lives_alone = bool(re.search(r"一人暮らし|ひとり暮らし|独居|lives alone|living alone", t))
    lang = next((code for code, pat in LANG_KW if re.search(pat, t)), None)
    if lang is None:
        ascii_ratio = sum(c.isascii() for c in text) / max(1, len(text))
        lang = "en" if ascii_ratio > 0.9 else "ja"
    if lang != "ja":
        needs.append("non_japanese_speaker")
    req = {"walk": 1, "walker": 2, "wheelchair": 2, "bedridden": 3}[mobility]
    if mobility == "walk" and floor >= 3 and re.search(r"エレベーターなし|エレベーターが無|no elevator|no lift", t):
        req = 2
    return IntakeResult(needs=needs, mobility=mobility, helpers_required=req, floor=floor,
                        lives_alone=lives_alone, age=age, language=lang, confidence=0.6)


async def parse_note(text: str) -> tuple[IntakeResult, bool]:
    """Returns (result, used_fallback)."""
    s = get_settings()
    obj, fb = await llm.complete_json(model=s.parse_model, system=SYSTEM,
                                      user=USER_TMPL.format(note=text[:1200]),
                                      schema=IntakeResult, max_tokens=400)
    if fb or obj is None:
        return keyword_fallback(text), True
    res: IntakeResult = obj  # type: ignore[assignment]
    # belt and braces: mobility must be reflected in needs
    if res.mobility != "walk" and res.mobility not in res.needs:
        res.needs.insert(0, res.mobility)
    if res.language != "ja" and "non_japanese_speaker" not in res.needs:
        res.needs.append("non_japanese_speaker")
    return res, False
