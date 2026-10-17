"""LLM client for the ai& OpenAI-compatible API. Built from scratch on the
`openai` SDK with a custom base_url.

Guarantees for every call site:
  * per-call timeout (settings.llm_timeout_s) + exactly 1 retry
  * concurrency capped by a semaphore
  * strict JSON mode when the endpoint supports it (probed at startup),
    otherwise robust extraction + repair
  * pydantic validation of every JSON response
  * a deterministic fallback at every call site — results are flagged
    fallback=True so the UI can label them
  * golden-path disk cache keyed by sha256(task inputs) so a rehearsed
    demo never stalls on the network
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import BaseModel

from .config import Settings, get_settings

CACHE_PATH = Path("data/llm_cache.json")


class LLMResult(BaseModel):
    text: str
    fallback: bool = False
    model: str = ""
    latency_ms: int = 0
    cached: bool = False


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._client: AsyncOpenAI | None = None
        if settings.llm_configured:
            self._client = AsyncOpenAI(
                base_url=settings.aiand_base_url,
                api_key=settings.aiand_api_key,
                timeout=settings.llm_timeout_s,
                max_retries=0,  # we do our own single retry
            )
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self.strict_json_ok = False
        self._cache: dict[str, str] = self._load_cache()

    # ----- cache -------------------------------------------------------
    def _load_cache(self) -> dict[str, str]:
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def _key(model: str, system: str, user: str, max_tokens: int, json_mode: bool) -> str:
        blob = json.dumps(
            [model, system, user, max_tokens, json_mode], ensure_ascii=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ----- core --------------------------------------------------------
    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 800,
        json_mode: bool = False,
    ) -> LLMResult:
        key = self._key(model, system, user, max_tokens, json_mode)
        if key in self._cache:
            return LLMResult(text=self._cache[key], model=model, cached=True)
        if self.s.llm_mode == "off" or self._client is None:
            return LLMResult(text="", fallback=True, model=model)

        kwargs: dict = {}
        if json_mode and self.strict_json_ok:
            kwargs["response_format"] = {"type": "json_object"}
        extra = self._no_thinking(model)
        if extra:
            kwargs["extra_body"] = extra

        started = time.monotonic()
        async with self._sem:
            for attempt in range(2):  # initial try + 1 retry
                try:
                    resp = await self._client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=max_tokens,
                        temperature=0.2,
                        **kwargs,
                    )
                    text = (resp.choices[0].message.content or "").strip()
                    if not text:
                        raise ValueError("empty completion")
                    latency = int((time.monotonic() - started) * 1000)
                    self._cache[key] = text
                    self._save_cache()
                    return LLMResult(text=text, model=model, latency_ms=latency)
                except Exception:
                    if attempt == 1:
                        break
                    await asyncio.sleep(0.8)
        return LLMResult(text="", fallback=True, model=model)

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int = 800,
    ) -> tuple[BaseModel | None, bool]:
        """Returns (validated_obj, used_fallback). Caller applies its own
        deterministic fallback when used_fallback is True."""
        res = await self.complete(
            model=model, system=system, user=user, max_tokens=max_tokens, json_mode=True
        )
        if res.fallback:
            return None, True
        obj = self._parse_json(res.text, schema)
        if obj is None:
            return None, True
        return obj, False

    @staticmethod
    def _no_thinking(model: str) -> dict | None:
        """Reasoning models on ai& spend max_tokens on hidden thinking and return
        empty content; these short structured tasks don't need it."""
        m = model.lower()
        if "deepseek" in m:
            return {"chat_template_kwargs": {"thinking": False}}
        if "qwen" in m:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        if "gpt-oss" in m:
            return {"reasoning_effort": "low"}
        return None

    # ----- JSON extraction + repair ------------------------------------
    @staticmethod
    def _parse_json(text: str, schema: type[BaseModel]) -> BaseModel | None:
        candidate = LLMClient._extract_json_object(text)
        if candidate is None:
            return None
        for fixer in (lambda s: s, LLMClient._repair):
            try:
                return schema.model_validate(json.loads(fixer(candidate)))
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        # strip code fences
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    @staticmethod
    def _repair(s: str) -> str:
        s = re.sub(r",(\s*[}\]])", r"\1", s)  # trailing commas
        s = s.replace("\u201c", '"').replace("\u201d", '"')  # smart quotes
        return s

    # ----- startup probes ----------------------------------------------
    async def ping_models(self) -> dict[str, bool]:
        """1-token warmup per configured model + strict-JSON probe."""
        models = {self.s.parse_model, self.s.plan_model, self.s.msg_model, self.s.report_model, self.s.resident_model}
        out: dict[str, bool] = {}
        if self._client is None or self.s.llm_mode == "off":
            return {m: False for m in models}
        for m in models:
            try:
                resp = await self._client.chat.completions.create(
                    model=m,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    timeout=10,
                )
                out[m] = bool(resp.choices)
            except Exception:
                out[m] = False
        try:
            await self._client.chat.completions.create(
                model=self.s.parse_model,
                messages=[{"role": "user", "content": 'Return {"ok": true} as JSON.'}],
                max_tokens=20,
                timeout=10,
                response_format={"type": "json_object"},
            )
            self.strict_json_ok = True
        except Exception:
            self.strict_json_ok = False
        return out

    async def list_models(self) -> list[str]:
        if self._client is None:
            return []
        try:
            page = await self._client.models.list()
            return sorted(m.id for m in page.data)
        except Exception:
            return []


llm = LLMClient(get_settings())
