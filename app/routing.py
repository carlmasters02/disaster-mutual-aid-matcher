"""In-memory road graph (networkx) — the live routing engine.

Neo4j is the system of record; this mirror exists because a quake tick needs
hundreds of shortest paths and Aura round-trips would stall the demo.
`verify_apoc_parity` proves both engines agree when APOC is available.
"""
from dataclasses import dataclass, field

import networkx as nx

from .models import Intersection, Road

WHEELCHAIR_MIN_WIDTH = 2.5
WHEELCHAIR_MAX_WALL_RISK = 0.7


@dataclass
class RouteResult:
    path: list[str]
    eta_min: float
    road_ids: list[str] = field(default_factory=list)


class RoadGraph:
    def __init__(self, intersections: dict[str, Intersection], roads: dict[str, Road]) -> None:
        self.ints = intersections
        self.rebuild(roads)

    def rebuild(self, roads: dict[str, Road]) -> None:
        """Full rebuild — ~300 segments, microseconds. Called after every wave."""
        g, gw = nx.DiGraph(), nx.DiGraph()
        g.add_nodes_from(self.ints)
        gw.add_nodes_from(self.ints)
        for r in roads.values():
            if r.blocked or r.on_fire:
                continue
            g.add_edge(r.from_id, r.to_id, w=r.minutes, id=r.id)
            if r.width_m >= WHEELCHAIR_MIN_WIDTH and r.block_wall_risk <= WHEELCHAIR_MAX_WALL_RISK:
                gw.add_edge(r.from_id, r.to_id, w=r.minutes, id=r.id)
        self.g, self.gw = g, gw
        self._dist_cache: dict[tuple[str, bool, float | None], dict[str, float]] = {}
        self._path_cache: dict[tuple[str, bool], tuple[dict, dict]] = {}

    def _graph(self, wheelchair: bool) -> nx.DiGraph:
        return self.gw if wheelchair else self.g

    def dists_from(self, src: str, *, wheelchair: bool = False, cutoff: float | None = None) -> dict[str, float]:
        key = (src, wheelchair, cutoff)
        if key not in self._dist_cache:
            self._dist_cache[key] = nx.single_source_dijkstra_path_length(
                self._graph(wheelchair), src, cutoff=cutoff, weight="w")
        return self._dist_cache[key]

    def paths_from(self, src: str, *, wheelchair: bool = False) -> tuple[dict, dict]:
        key = (src, wheelchair)
        if key not in self._path_cache:
            self._path_cache[key] = nx.single_source_dijkstra(self._graph(wheelchair), src, weight="w")
        return self._path_cache[key]

    def route(self, a: str, b: str, *, wheelchair: bool = False) -> RouteResult | None:
        dist, paths = self.paths_from(a, wheelchair=wheelchair)
        if b not in dist:
            return None
        path = paths[b]
        g = self._graph(wheelchair)
        road_ids = [g.edges[u, v]["id"] for u, v in zip(path, path[1:])]
        return RouteResult(path=path, eta_min=round(dist[b], 2), road_ids=road_ids)


async def verify_apoc_parity(rg: RoadGraph, samples: int = 10) -> dict:
    """Compare APOC dijkstra on Aura against the in-memory engine."""
    from . import db

    if not db.db_ok():
        return {"checked": 0, "agree": 0, "engine": "networkx-only"}
    nodes = sorted(rg.ints)
    agree = checked = 0
    for k in range(samples):
        a, b = nodes[(k * 37) % len(nodes)], nodes[(k * 91 + 13) % len(nodes)]
        local = rg.route(a, b)
        try:
            rows = await db.run_query(
                "MATCH (s:Intersection {id:$a}), (t:Intersection {id:$b}) "
                "CALL apoc.algo.dijkstra(s, t, 'ROAD>', 'minutes_eff') YIELD weight "
                "RETURN weight", a=a, b=b)
        except Exception:
            return {"checked": checked, "agree": agree, "engine": "apoc-unavailable"}
        checked += 1
        if rows and local and abs(rows[0]["weight"] - local.eta_min) < 0.05:
            agree += 1
    return {"checked": checked, "agree": agree, "engine": "apoc+networkx"}
