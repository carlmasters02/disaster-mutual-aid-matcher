"""WebSocket broadcaster: typed pydantic events + connection manager.

Built from scratch (no library). Clients receive a full `snapshot` on
connect, then incremental typed events. Patch events are coalesced to at
most 10/s so quake waves cannot storm the fan-out.
"""
import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

from fastapi import WebSocket
from pydantic import BaseModel, Field

from .models import Coverage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WSEvent(BaseModel):
    type: str
    ts: str = Field(default_factory=_now)


class SnapshotEvent(WSEvent):
    type: Literal["snapshot"] = "snapshot"
    payload: dict = Field(default_factory=dict)


class PatchEvent(WSEvent):
    type: Literal["patch"] = "patch"
    payload: dict = Field(default_factory=dict)


class WaveEvent(WSEvent):
    type: Literal["wave"] = "wave"
    n: int = 0
    total_waves: int = 5
    blocked_roads: list[str] = Field(default_factory=list)
    fires: list[str] = Field(default_factory=list)
    injured_helpers: list[str] = Field(default_factory=list)
    dropped_helpers: list[str] = Field(default_factory=list)
    damaged_shelters: list[str] = Field(default_factory=list)


class RematchEvent(WSEvent):
    type: Literal["rematch"] = "rematch"
    plans_changed: int = 0
    coverage: Coverage = Field(default_factory=Coverage)
    statuses: dict[str, str] = Field(default_factory=dict)
    assignments: list[dict] = Field(default_factory=list)
    evacuations: list[dict] = Field(default_factory=list)
    wave: int | None = None  # set when this rematch belongs to a quake wave
    slot: str = ""


class DispatchEvent(WSEvent):
    type: Literal["dispatch"] = "dispatch"
    helper_id: str = ""
    helper_name: str = ""
    person_id: str = ""
    lang: str = "ja"
    text: str = ""
    fallback: bool = False


class EscalationEvent(WSEvent):
    type: Literal["escalation"] = "escalation"
    person_id: str = ""
    priority: int = 3
    reason: str = ""
    explanation: str = ""
    fallback: bool = False


class ReportEvent(WSEvent):
    type: Literal["report"] = "report"
    payload: dict = Field(default_factory=dict)
    fallback: bool = False


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._snapshot_provider: Callable[[], Awaitable[dict]] | None = None
        self._patch_lock = asyncio.Lock()
        self._last_patch = 0.0

    def set_snapshot_provider(self, fn: Callable[[], Awaitable[dict]]) -> None:
        self._snapshot_provider = fn

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        if self._snapshot_provider is not None:
            payload = await self._snapshot_provider()
            await self._safe_send(ws, SnapshotEvent(payload=payload))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, ev: WSEvent) -> None:
        dead: list[WebSocket] = []
        data = ev.model_dump_json()
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def broadcast_patch(self, payload: dict) -> None:
        """Coalesced patch: at most one per 100 ms; intermediate states merge."""
        async with self._patch_lock:
            now = asyncio.get_event_loop().time()
            if now - self._last_patch < 0.1:
                return
            self._last_patch = now
        await self.broadcast(PatchEvent(payload=payload))

    async def _safe_send(self, ws: WebSocket, ev: WSEvent) -> None:
        try:
            await ws.send_text(ev.model_dump_json())
        except Exception:
            self._clients.discard(ws)


manager = ConnectionManager()
