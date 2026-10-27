"""Durable reservations at the HTTPX transport boundary, not at the agent loop.

Only MockTransport can be attached in this release. Unknown requests are never
replayed. A new process must reconcile/abandon its run instead of releasing quota.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class BudgetStop(RuntimeError):
    pass


@dataclass(frozen=True)
class Scope:
    run: str
    request_id: str
    kind: str = "ordinary"


CURRENT: ContextVar[Scope | None] = ContextVar("pilot_physical_request", default=None)


@contextmanager
def request_scope(run: str, request_id: str, kind: str = "ordinary"):
    token = CURRENT.set(Scope(run, request_id, kind))
    try:
        yield
    finally:
        CURRENT.reset(token)


class Ledger:
    def __init__(self, path: Path, *, total_cap: int = 192) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS contract (id INTEGER PRIMARY KEY, cap INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (run TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS requests (
                    seq INTEGER PRIMARY KEY, run TEXT NOT NULL, request_id TEXT NOT NULL,
                    kind TEXT NOT NULL, sha256 TEXT NOT NULL, input_bound INTEGER NOT NULL,
                    state TEXT NOT NULL, usage TEXT, error TEXT,
                    UNIQUE(run, request_id));
            """)
            db.execute("INSERT OR IGNORE INTO contract VALUES (1, ?)", (total_cap,))
            if db.execute("SELECT cap FROM contract").fetchone()[0] != total_cap:
                raise BudgetStop("frozen_budget_contract_mismatch")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def register(self, run: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE run=?", (run,)).fetchone():
                if db.execute("SELECT count(*) FROM runs").fetchone()[0] >= 24:
                    db.execute("ROLLBACK")
                    raise BudgetStop("pilot_run_count_exceeded")
                db.execute("INSERT INTO runs VALUES (?)", (run,))
            db.execute("COMMIT")

    def rows(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY seq")]

    def reserve(self, request: httpx.Request) -> int:
        scope = CURRENT.get()
        if scope is None or scope.kind not in {"ordinary", "maintenance"}:
            raise BudgetStop("missing_or_invalid_request_scope")
        if request.url.host != "provider.invalid" or request.method != "POST":
            raise BudgetStop("offline_endpoint_only")
        body = request.content
        payload = json.loads(body)
        if payload.get("stream") is not False:
            raise BudgetStop("streaming_not_in_frozen_contract")
        if payload.get("max_completion_tokens") != 16384:
            raise BudgetStop("output_contract_mismatch")
        # UTF-8 bytes are an explicitly conservative input proxy, never native usage.
        bound = len(body)
        if bound > 131072:
            raise BudgetStop("estimated_input_envelope")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if not db.execute("SELECT 1 FROM runs WHERE run=?", (scope.run,)).fetchone():
                    raise BudgetStop("unregistered_run")
                rows = db.execute("SELECT * FROM requests WHERE run=?", (scope.run,)).fetchall()
                if any(r["request_id"] == scope.request_id for r in rows):
                    raise BudgetStop("request_already_reserved_no_replay")
                if any(r["state"] != "complete" for r in rows):
                    raise BudgetStop("unresolved_or_failed_request_stop_run")
                cap = db.execute("SELECT cap FROM contract").fetchone()[0]
                if db.execute("SELECT count(*) FROM requests").fetchone()[0] >= cap:
                    raise BudgetStop("pilot_physical_request_budget")
                if sum(r["kind"] == scope.kind for r in rows) >= (
                    6 if scope.kind == "ordinary" else 2
                ):
                    raise BudgetStop("run_physical_request_budget")
                used = sum(json.loads(r["usage"])["total_tokens"] for r in rows)
                if used + bound + 16384 > 262144:
                    raise BudgetStop("native_token_remaining_insufficient")
                cursor = db.execute(
                    "INSERT INTO requests(run,request_id,kind,sha256,input_bound,state) VALUES(?,?,?,?,?,?)",
                    (
                        scope.run,
                        scope.request_id,
                        scope.kind,
                        hashlib.sha256(body).hexdigest(),
                        bound,
                        "possibly_sent",
                    ),
                )
                seq = cursor.lastrowid
                db.execute("COMMIT")  # Durable BEFORE delegating to physical transport.
                assert seq is not None
                return seq
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def finish(
        self, seq: int, *, response: httpx.Response | None = None, error: str | None = None
    ) -> None:
        state, usage = "unknown_response", None
        if response is not None:
            if response.status_code >= 400:
                state, error = "provider_error", f"http_{response.status_code}"
            else:
                try:
                    packet = response.json()
                    native = packet.get("usage")
                    choices = packet.get("choices")
                    valid_choices = (
                        isinstance(choices, list)
                        and bool(choices)
                        and isinstance(choices[0].get("message"), dict)
                    )
                    valid_usage = isinstance(native, dict) and all(
                        type(native.get(k)) is int and native[k] >= 0
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                    )
                    if (
                        valid_usage
                        and native["total_tokens"]
                        != native["prompt_tokens"] + native["completion_tokens"]
                    ):
                        valid_usage = False
                    usage = json.dumps(native) if valid_usage else None
                    state = (
                        "complete"
                        if valid_choices and valid_usage
                        else "invalid_response"
                        if not valid_choices
                        else "usage_missing"
                    )
                except (ValueError, TypeError, AttributeError, KeyError):
                    state = "invalid_response"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE seq=?", (seq,)).fetchone()
            if row is None or row["state"] != "possibly_sent":
                db.execute("ROLLBACK")
                raise BudgetStop("invalid_receipt_transition")
            if usage is not None:
                others = db.execute(
                    "SELECT usage FROM requests WHERE run=? AND usage IS NOT NULL", (row["run"],)
                ).fetchall()
                used = sum(json.loads(r["usage"])["total_tokens"] for r in others)
                if used + json.loads(usage)["total_tokens"] > 262144:
                    state = "native_token_exhausted"
            db.execute(
                "UPDATE requests SET state=?,usage=?,error=? WHERE seq=?",
                (state, usage, error, seq),
            )
            db.execute("COMMIT")
        if response is not None and state != "complete" and response.status_code < 400:
            raise BudgetStop(state)


class GuardTransport(httpx.BaseTransport):
    def __init__(self, ledger: Ledger, inner: httpx.MockTransport):
        if not isinstance(inner, httpx.MockTransport):
            raise BudgetStop("live_transport_disabled")
        self.ledger, self.inner = ledger, inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        seq = self.ledger.reserve(request)
        try:
            response = self.inner.handle_request(request)
            response.read()
        except BaseException as exc:
            self.ledger.finish(seq, error=type(exc).__name__)
            raise
        self.ledger.finish(seq, response=response)
        return response


class AsyncGuardTransport(httpx.AsyncBaseTransport):
    def __init__(self, ledger: Ledger, inner: httpx.MockTransport):
        if not isinstance(inner, httpx.MockTransport):
            raise BudgetStop("live_transport_disabled")
        self.ledger, self.inner = ledger, inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        seq = self.ledger.reserve(request)
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
        except BaseException as exc:
            self.ledger.finish(seq, error=type(exc).__name__)
            raise
        self.ledger.finish(seq, response=response)
        return response
