"""Deterministic views of one authoritative Session graph snapshot. No history writes."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from agent_workbench.contracts.graph_read import GraphQuery, GraphRead
from agent_workbench.domain.session_graph import read_graph
from agent_workbench.models.context_budget import estimate_value, serialized

RULE = "Stable (graph, kind, persistent ID) order; categories filter stored status/outcome only. No importance ranking."
HINT = "Overview is partial. graph_query can search all IDs/text/status/outcome or page the entire graph; read_shared_graph requests full when budget permits."
EVIDENCE = "source_refs are provenance, not verification. artifact:<UUID>: read_artifact (Main) / read_evidence (Worker). operation:<UUID>: search_history(kind=operation, query=UUID) (Main) / list_evidence(source_operation_id=UUID), then read_evidence (Worker); operation is not an Artifact UUID."


def records(snapshot: dict, attack_enabled: bool = True) -> list[dict]:
    return sorted(
        [{"graph": graph, "kind": kind, "data": item}
         for graph in ("information", "attack") if graph != "attack" or attack_enabled
         for kind in ("nodes", "edges")
         for item in snapshot.get(graph + "_graph", {}).get(kind, [])],
        key=lambda r: (r["graph"], r["kind"], r["data"]["id"]),
    )


def envelope(snapshot: dict, enabled: bool, attack_enabled: bool) -> dict:
    return {"revision": snapshot["revision"], "enabled": enabled,
            "attack_enabled": attack_enabled, "evidence_hint": EVIDENCE}


def overview(snapshot: dict, *, enabled: bool = True, attack_enabled: bool = True,
             category_limit: int = 4, token_budget: int = 4096) -> dict:
    all_records = records(snapshot, attack_enabled) if enabled else []
    counts = Counter((r["graph"], r["kind"]) for r in all_records)
    relations = Counter(r["data"].get("status") for r in all_records
                        if r["graph"] == "information" and r["kind"] == "edges")
    attempts = Counter(r["data"].get("outcome") for r in all_records
                       if r["graph"] == "attack" and r["kind"] == "edges")
    entities = {r["data"]["id"] for r in all_records if r["graph"] == "information" and r["kind"] == "nodes"}
    categories = {}
    for graph, field, values in (("information", "status", ("hypothesis", "refuted")),
                                  ("attack", "outcome", ("success", "blocked", "unknown"))):
        for value in values:
            matched = [r for r in all_records if r["graph"] == graph and r["kind"] == "edges" and r["data"].get(field) == value]
            categories[value] = {"total": len(matched), "items": matched[:category_limit],
                                 "query": {"graph": graph, "kind": "edges", field: value}}
    result = {**envelope(snapshot, enabled, attack_enabled), "mode": "overview", "rule": RULE,
              "note": HINT, "counts": {
                  "entities": counts["information", "nodes"], "relations": counts["information", "edges"],
                  "relation_status": {v: relations[v] for v in ("observed", "hypothesis", "refuted", "superseded")},
                  "attack_states": counts["attack", "nodes"], "attempts": counts["attack", "edges"],
                  "attempt_outcome": {v: attempts[v] for v in ("success", "negative", "blocked", "unknown")},
                  "linked_attack_states": sum(bool(entities.intersection(r["data"].get("entity_refs", []))) for r in all_records if r["graph"] == "attack" and r["kind"] == "nodes")},
              "categories": categories, "endpoints": [], "total": len(all_records)}

    def refresh():
        selected = {(r["graph"], r["data"][f]) for c in categories.values() for r in c["items"] for f in ("from_id", "to_id")}
        endpoints = [r for r in all_records if r["kind"] == "nodes" and (r["graph"], r["data"]["id"]) in selected]
        refs = {ref for r in endpoints if r["graph"] == "attack" for ref in r["data"].get("entity_refs", [])}
        result["endpoints"] = [r for r in all_records if r in endpoints or (r["graph"] == "information" and r["kind"] == "nodes" and r["data"]["id"] in refs)]
        for c in categories.values():
            c.update(displayed=len(c["items"]), omitted=c["total"] - len(c["items"]))
        result["displayed"] = len(result["endpoints"]) + sum(c["displayed"] for c in categories.values())
        result["omitted"] = result["total"] - result["displayed"]

    refresh()
    while estimate_value(result) > token_budget:
        nonempty = [c for c in categories.values() if c["items"]]
        if not nonempty:
            return omitted(snapshot, enabled, attack_enabled)
        max(nonempty, key=lambda c: len(c["items"]))["items"].pop()
        refresh()
    return result


def omitted(snapshot: dict, enabled: bool, attack_enabled: bool) -> dict:
    return {"revision": snapshot["revision"], "enabled": enabled, "attack_enabled": attack_enabled,
            "mode": "omitted", "note": "Graph body exceeds available budget; graph_overview / graph_query / read_shared_graph remain available. History is not acknowledged."}


def adaptive(snapshot: dict, token_budget: int, *, enabled: bool = True, attack_enabled: bool = True) -> dict:
    graph = {k: v for k, v in snapshot.items() if k != "attack_graph" or attack_enabled}
    result = {**envelope(snapshot, enabled, attack_enabled), "mode": "full", "graph": graph}
    if enabled and estimate_value(result) <= token_budget:
        return result
    return overview(snapshot, enabled=enabled, attack_enabled=attack_enabled, token_budget=token_budget)


def query_graph(snapshot: dict, value: GraphQuery, *, scope: str = "", enabled: bool = True,
                attack_enabled: bool = True) -> dict:
    base = envelope(snapshot, enabled, attack_enabled)
    def error(code: str):
        return {**base, "error": code, "records": [], "total": 0, "has_more": False,
                "recovery": "Restart query without cursor at current revision; increase token_budget if needed."}
    if value.expected_revision is not None and value.expected_revision != snapshot["revision"]:
        return error("stale_revision")
    fingerprint = hashlib.sha256(serialized([scope, enabled, attack_enabled, value.model_dump(exclude={"cursor", "token_budget", "limit", "expected_revision"})]).encode()).hexdigest()[:24]
    offset = 0
    if value.cursor:
        try:
            rev, digest, offset = json.loads(base64.urlsafe_b64decode(value.cursor.encode()))
            if rev != snapshot["revision"]:
                return error("stale_cursor")
            if digest != fingerprint or type(offset) is not int or offset < 0:
                return error("invalid_cursor")
        except (ValueError, TypeError, UnicodeError):
            return error("invalid_cursor")
    all_records = records(snapshot, attack_enabled) if enabled else []

    def matches(r):
        d = r["data"]
        return ((value.graph == "both" or value.graph == r["graph"])
                and (value.kind == "both" or value.kind == r["kind"])
                and (not value.ids or d["id"] in value.ids)
                and (not value.keyword or value.keyword.casefold() in " ".join(str(d.get(f, "")) for f in ("label", "summary", "relation", "action")).casefold())
                and (value.status is None or d.get("status") == value.status)
                and (value.outcome is None or d.get("outcome") == value.outcome))
    matched = [r for r in all_records if matches(r)]
    if value.expand or value.cross_graph:
        by_key = {(r["graph"], r["kind"], r["data"]["id"]): r for r in all_records}
        adjacency: dict[tuple, set] = defaultdict(set)
        for key, r in by_key.items():
            d, graph, kind = r["data"], r["graph"], r["kind"]
            if kind == "edges" and value.expand:
                if value.status and graph == "information" and d.get("status") != value.status:
                    continue
                if value.outcome and graph == "attack" and d.get("outcome") != value.outcome:
                    continue
                a, b = (graph, "nodes", d["from_id"]), (graph, "nodes", d["to_id"])
                if value.direction in ("out", "both"):
                    adjacency[a].update((key, b))
                if value.direction in ("in", "both"):
                    adjacency[b].update((key, a))
                adjacency[key].update((a, b))
            if kind == "nodes" and graph == "attack" and value.cross_graph:
                for ref in d.get("entity_refs", []):
                    entity = ("information", "nodes", ref)
                    if entity in by_key:
                        adjacency[key].add(entity)
                        adjacency[entity].add(key)
        # Seeds are not restricted by edge-only filters, which apply to traversed edges.
        seeds = {k for k in by_key if k[2] in value.ids and (value.graph == "both" or k[0] == value.graph)}
        visited, frontier = set(seeds), set(seeds)
        for _ in range(value.hops):
            following = {n for k in frontier for n in adjacency[k] if n in by_key} - visited
            visited.update(following)
            frontier = following
        matched = [r for k, r in by_key.items() if k in visited]
    total = len(matched)
    if offset > total:
        return error("invalid_cursor")
    result: dict[str, Any] = {**base, "records": [], "total": total, "displayed": 0, "omitted": total,
                              "has_more": offset < total, "next_cursor": None, "offset": offset,
                              "order": "graph,kind,id", "token_estimation": "utf8_bytes_div_3"}
    def refresh():
        end = offset + len(result["records"])
        result.update(displayed=len(result["records"]), omitted=total - len(result["records"]), has_more=end < total,
                      next_cursor=base64.urlsafe_b64encode(json.dumps([snapshot["revision"], fingerprint, end]).encode()).decode() if end < total else None)
    for r in matched[offset:offset + value.limit]:
        result["records"].append(r)
        refresh()
        if estimate_value(result) > value.token_budget:
            result["records"].pop()
            refresh()
            break
    if offset < total and not result["records"]:
        return error("result_exceeds_budget")
    return result


async def read_shared(db: Any, session_id: Any, value: GraphRead, settings: Any) -> dict:
    snapshot = await read_graph(db, session_id)
    flags = {"enabled": settings.session_graph_enabled, "attack_enabled": settings.graph_attack_enabled}
    if value.action == "query":
        return query_graph(snapshot, value.query, scope=str(session_id), **flags)
    if value.action == "read":
        return adaptive(snapshot, value.token_budget, **flags)
    return overview(snapshot, token_budget=value.token_budget, category_limit=value.category_limit, **flags)
