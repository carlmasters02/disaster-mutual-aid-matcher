"""Earthquake simulation: deterministic intensity model + precomputed waves.

All randomness is seeded from (seed, magnitude, epicenter) so the golden
scenario replays identically (and hits the LLM cache).
"""
import math
import random

from pydantic import BaseModel, Field

from .generator import haversine_m
from .models import Helper, Intersection, Road, Shelter, TimeSlot

N_WAVES = 5


class QuakeParams(BaseModel):
    magnitude: float = 7.0
    epicenter_lat: float
    epicenter_lon: float


class WaveEffect(BaseModel):
    n: int
    blocked_roads: list[str] = Field(default_factory=list)  # directed ids (both directions)
    fires_new: list[str] = Field(default_factory=list)
    fires_out: list[str] = Field(default_factory=list)
    injured_helpers: list[str] = Field(default_factory=list)
    damaged_shelters: list[str] = Field(default_factory=list)
    occupancy_add: dict[str, int] = Field(default_factory=dict)
    power_out: bool = False


def intensity_at(mag: float, d_km: float) -> float:
    return mag - 3.0 * math.log10(d_km + 1.0)


def block_prob(i: float, r: Road) -> float:
    return max(0.0, min(0.9, 0.24 * (i - 4.2))) * (0.5 * r.block_wall_risk + 0.5 * r.wooden_density)


def _seg(rid: str) -> str:
    return rid[:-1] if rid.endswith("r") else rid


def plan_waves(params: QuakeParams, ints: dict[str, Intersection], roads: dict[str, Road],
               helpers: dict[str, Helper], shelters: dict[str, Shelter], seed: int) -> list[WaveEffect]:
    rng = random.Random(f"{seed}:{params.magnitude:.1f}:{params.epicenter_lat:.4f}:{params.epicenter_lon:.4f}")
    elat, elon = params.epicenter_lat, params.epicenter_lon
    waves = [WaveEffect(n=k + 1) for k in range(N_WAVES)]
    waves[0].power_out = params.magnitude >= 6.0

    # segments (undirected) with midpoint distance
    segs: dict[str, tuple[Road, float]] = {}
    for r in roads.values():
        sid = _seg(r.id)
        if sid in segs:
            continue
        a, b = ints[r.from_id], ints[r.to_id]
        d = haversine_m((a.lat + b.lat) / 2, (a.lon + b.lon) / 2, elat, elon)
        segs[sid] = (r, d)

    # 1. road blockages — rolled once, revealed outward in 5 waves
    blocked = [(d, sid) for sid, (r, d) in sorted(segs.items())
               if rng.random() < block_prob(intensity_at(params.magnitude, d / 1000), r)]
    blocked.sort()
    for k, (d, sid) in enumerate(blocked):
        w = min(N_WAVES - 1, int(k * N_WAVES / max(1, len(blocked))))
        waves[w].blocked_roads += [sid, sid + "r"]

    # 2. fires — ignite in dense wooden cells near the epicenter, spread 2 waves
    adj: dict[str, set[str]] = {}
    for sid, (r, _) in segs.items():
        for node in (r.from_id, r.to_id):
            adj.setdefault(node, set()).add(sid)
    near = [sid for sid, (r, d) in sorted(segs.items()) if d < 700 and r.wooden_density > 0.7]
    n_ign = 0 if params.magnitude < 6.0 else min(len(near), 2 + int(params.magnitude >= 6.8) + int(params.magnitude >= 7.2))
    burning: dict[str, int] = {sid: 0 for sid in rng.sample(near, n_ign)}  # sid -> age
    waves[0].fires_new += [x for s in burning for x in (s, s + "r")]
    ever = set(burning)
    for w in range(1, N_WAVES):
        new = []
        for sid, age in list(burning.items()):
            if age < 2:
                r = segs[sid][0]
                for node in (r.from_id, r.to_id):
                    for nb in sorted(adj[node]):
                        if nb not in ever and segs[nb][0].wooden_density > 0.65 and rng.random() < 0.38:
                            new.append(nb)
                            ever.add(nb)
            burning[sid] = age + 1
        for sid in [s for s, a in burning.items() if a >= 3]:
            waves[w].fires_out += [sid, sid + "r"]
            del burning[sid]
        for sid in new:
            burning[sid] = 0
        waves[w].fires_new += [x for s in new for x in (s, s + "r")]

    # 3. injured helpers (~5%, weighted toward the epicenter), mostly wave 1-2
    for h in sorted(helpers.values(), key=lambda h: h.id):
        i = intensity_at(params.magnitude, haversine_m(h.lat, h.lon, elat, elon) / 1000)
        if rng.random() < max(0.0, 0.035 * (i - 4.5)):
            waves[0 if rng.random() < 0.6 else 1].injured_helpers.append(h.id)

    # 4. shelters: damage in wave 2, evacuee arrivals every wave
    for s in sorted(shelters.values(), key=lambda s: s.id):
        i = intensity_at(params.magnitude, haversine_m(s.lat, s.lon, elat, elon) / 1000)
        if rng.random() < max(0.0, 0.12 * (i - 5.0)):
            waves[1].damaged_shelters.append(s.id)
        if s.kind == "指定避難所":
            frac = 0.07 + 0.08 * max(0.0, i - 5.5)
            for w in waves:
                w.occupancy_add[s.id] = int(s.capacity * frac)
    return waves


def apply_effects(w: WaveEffect, roads: dict[str, Road], helpers: dict[str, Helper],
                  shelters: dict[str, Shelter]) -> None:
    """Mutate world state in place. Burnt-out roads stay blocked (debris)."""
    for rid in w.blocked_roads:
        roads[rid].blocked = True
    for rid in w.fires_new:
        roads[rid].on_fire = True
    for rid in w.fires_out:
        roads[rid].on_fire = False
        roads[rid].blocked = True
    for hid in w.injured_helpers:
        helpers[hid].injured = True
        helpers[hid].available = False
    for sid in w.damaged_shelters:
        shelters[sid].damaged = True
    for sid, add in w.occupancy_add.items():
        if not shelters[sid].damaged:
            shelters[sid].occupancy = min(int(shelters[sid].capacity * 1.1), shelters[sid].occupancy + add)


def unavailable_in_slot(helpers: dict[str, Helper], slot: TimeSlot) -> list[str]:
    return [h.id for h in helpers.values() if slot not in h.availability]
