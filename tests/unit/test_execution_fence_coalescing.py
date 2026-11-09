"""Equivalent CAS acknowledgements must not cause six-way dispatch starvation."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from workbench_executor.ledger import Ledger

from agent_workbench.tools.execution_dispatch import ExecutionUnavailable, sync_fence


def make_box(monkeypatch, remote):
    async def revocations(_db, _session):
        return []

    monkeypatch.setattr(
        "agent_workbench.domain.execution_delegations.revocation_payload", revocations
    )

    class Session:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(control_version=3))

    @asynccontextmanager
    async def session():
        yield Session()

    return SimpleNamespace(
        db=SimpleNamespace(session=session),
        session_id=uuid4(),
        run_id=uuid4(),
        owner="runner",
        owner_generation=2,
        control_version=3,
        execution_fence_seq=1,
        executor=remote,
    )


def ack(payload, *, applied=False, seq=2, **updates):
    fence = {
        k: deepcopy(payload[k])
        for k in [
            "session_id",
            "run_id",
            "owner",
            "owner_generation",
            "control_version",
            "revocations",
        ]
    }
    fence.update(seq=seq, **updates)
    return {
        "applied": applied,
        "reason": "" if applied else "expected_seq 落后于执行端当前 fence；请刷新后重试",
        "fence": fence,
    }


def test_six_parallel_commands_share_equivalent_ack_without_cas_retry(monkeypatch, tmp_path):
    async def scenario():
        gate = asyncio.Event()
        ledger = Ledger(tmp_path / "fences.sqlite3")
        await ledger.open()

        class Remote:
            def __init__(self):
                self.calls = []

            async def execution_sync_control(self, payload):
                self.calls.append(deepcopy(payload))
                index = len(self.calls)
                if index == 6:
                    gate.set()
                await gate.wait()
                fields = {
                    key: payload[key]
                    for key in [
                        "session_id",
                        "run_id",
                        "owner",
                        "owner_generation",
                        "control_version",
                        "revocations",
                        "expected_seq",
                    ]
                }
                applied, fence, reason = await ledger.sync_fence(**fields)
                return {"applied": applied, "fence": fence, "reason": reason}

        remote = Remote()
        box = make_box(monkeypatch, remote)
        try:
            await ledger.sync_fence(
                session_id=str(box.session_id),
                run_id=str(box.run_id),
                owner=box.owner,
                owner_generation=2,
                control_version=3,
                revocations=[],
                expected_seq=None,
            )
            assert await asyncio.gather(*(sync_fence(box) for _ in range(6))) == [2] * 6
            assert len(remote.calls) == 6
            assert box.execution_fence_seq == 2
        finally:
            await ledger.close()

    asyncio.run(scenario())


def test_newer_remote_revocation_is_preserved_without_a_rewrite(monkeypatch):
    async def scenario():
        ident = str(uuid4())

        class Remote:
            calls = 0

            async def execution_sync_control(self, payload):
                self.calls += 1
                return ack(payload, revocations=[{"delegation_id": ident, "revision": 2}])

        remote = Remote()
        box = make_box(monkeypatch, remote)
        assert await sync_fence(box, revocations=[{"delegation_id": ident, "revision": 1}]) == 2
        assert remote.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"control_version": 4},
        {"owner_generation": 3},
        {"run_id": "another-run"},
        {"owner": "another-owner"},
        {"reason": "disabled"},
    ],
)
def test_different_authority_or_rejection_is_never_treated_as_equivalent(monkeypatch, change):
    async def scenario():
        class Remote:
            async def execution_sync_control(self, payload):
                value = ack(payload, **{k: v for k, v in change.items() if k != "reason"})
                if "reason" in change:
                    value["reason"] = change["reason"]
                return value

        box = make_box(monkeypatch, Remote())
        with pytest.raises(ExecutionUnavailable, match="执行端拒绝"):
            await sync_fence(box)
        assert box.execution_fence_seq == 1

    asyncio.run(scenario())


def test_new_revocation_is_not_lost_when_the_remote_fence_does_not_cover_it(monkeypatch):
    async def scenario():
        wanted = {"delegation_id": str(uuid4()), "revision": 1}

        class Remote:
            def __init__(self):
                self.calls = []

            async def execution_sync_control(self, payload):
                self.calls.append(deepcopy(payload))
                return ack(
                    payload,
                    applied=len(self.calls) == 2,
                    seq=len(self.calls) + 1,
                    revocations=[] if len(self.calls) == 1 else [wanted],
                )

        remote = Remote()
        box = make_box(monkeypatch, remote)
        assert await sync_fence(box, revocations=[wanted]) == 3
        assert len(remote.calls) == 2 and remote.calls[1]["revocations"] == [wanted]

    asyncio.run(scenario())
