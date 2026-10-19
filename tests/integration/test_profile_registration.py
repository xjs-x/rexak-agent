"""冻结 profile：跨进程 HTTP 幂等、事务可见性与旧 hash 兼容。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

from agent_workbench.api.app import create_app
from agent_workbench.domain.coverage_checklist import CoverageProfile
from agent_workbench.domain.findings import CoverageService, profile_definition_hash
from agent_workbench.persistence.tables import CoverageProfileRow, SessionRow
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration
PROFILE = CoverageProfile("web", "v1", ("configuration", "auth"))
FINGERPRINT = "f" * 64


def _definition(profile: CoverageProfile = PROFILE) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "condition_fingerprint": FINGERPRINT,
        "categories": list(profile.check_categories),
        "include_parameter_cells": profile.include_parameter_cells,
        "include_anonymous": profile.include_anonymous,
    }


async def _register(db, session_id: UUID, profile: CoverageProfile = PROFILE):
    return await CoverageService(db).register_profile(
        session_id, profile=profile, condition_fingerprint=FINGERPRINT
    )


def _worker(session_id: str, directory: Path, worker_id: str, definition: str) -> None:
    """独立进程 / 事件循环 / 连接；只对 pytest 的随机库发 HTTP。"""
    from agent_workbench.config.settings import get_settings
    from agent_workbench.platform_compat import configure_event_loop_policy

    configure_event_loop_policy()
    settings = get_settings()
    if not str(make_url(settings.database_url).database).startswith("workbench_test_"):
        raise RuntimeError("profile worker 拒绝访问非隔离测试库")
    original = CoverageService.register_profile

    async def start_together(self, *args, **kwargs):
        (directory / f"ready-{worker_id}").write_text("ready", encoding="utf-8")
        async with asyncio.timeout(25):
            while not (directory / "go").exists():
                await asyncio.sleep(0.02)
        return await original(self, *args, **kwargs)

    CoverageService.register_profile = start_together
    with TestClient(create_app()) as client:
        client.post("/api/session").raise_for_status()
        response = client.post(
            f"/api/sessions/{session_id}/coverage/profile", json=json.loads(definition)
        )
        print(
            "PROFILE_RESULT="
            + json.dumps({"status": response.status_code, "body": response.json()})
        )


@pytest.mark.parametrize(
    "different", [False, True], ids=["same-definition", "conflicting-definition"]
)
async def test_two_processes_first_post(database, unique_name, tmp_path, different):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    profiles = [
        PROFILE,
        replace(PROFILE, check_categories=("configuration",)) if different else PROFILE,
    ]
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.integration.test_profile_registration",
                "--worker",
                str(session_id),
                str(tmp_path),
                str(index),
                json.dumps(_definition(profile)),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        for index, profile in enumerate(profiles)
    ]
    try:
        async with asyncio.timeout(25):
            while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
                assert all(worker.poll() is None for worker in workers), "HTTP worker 在屏障前退出"
                await asyncio.sleep(0.02)
        (tmp_path / "go").write_text("go", encoding="utf-8")
        responses = []
        for worker in workers:
            stdout, stderr = await asyncio.to_thread(worker.communicate, timeout=30)
            assert worker.returncode == 0, stderr.decode("utf-8")[-1500:]
            result_line = next(
                line
                for line in stdout.decode("utf-8").splitlines()
                if line.startswith("PROFILE_RESULT=")
            )
            responses.append(json.loads(result_line.removeprefix("PROFILE_RESULT=")))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()

    async with database.session() as db:
        rows = list(
            (
                await db.execute(
                    select(CoverageProfileRow).where(CoverageProfileRow.session_id == session_id)
                )
            ).scalars()
        )
    assert len(rows) == 1
    if different:
        assert sorted(item["status"] for item in responses) == [200, 409], responses
        winner = next(item["body"] for item in responses if item["status"] == 200)
        loser = next(item["body"] for item in responses if item["status"] == 409)
        assert "coverage_profile_conflict" in loser["message"]
        assert rows[0].categories == winner["categories"]
        assert rows[0].definition_hash == winner["definition_hash"]
    else:
        assert [item["status"] for item in responses] == [200, 200], responses
        assert responses[0]["body"]["profile_row_id"] == responses[1]["body"]["profile_row_id"]
        assert responses[0]["body"]["definition_hash"] == responses[1]["body"]["definition_hash"]
        assert str(rows[0].profile_row_id) == responses[0]["body"]["profile_row_id"]


async def test_rollback_then_retry_persists(database, unique_name):
    context = await context_for(database, unique_name)

    class RollbackProbe(Exception):
        pass

    first_id: UUID | None = None
    with pytest.raises(RollbackProbe):
        async with database.transaction() as db:
            first = await _register(db, context.toolbox.session_id)
            first_id = first.profile_row_id
            raise RollbackProbe
    async with database.transaction() as db:
        retry = await _register(db, context.toolbox.session_id)
        retry_id = retry.profile_row_id
    async with database.session() as db:
        stored = await CoverageService(db).get_profile(
            context.toolbox.session_id,
            profile_id="web",
            profile_version="v1",
            condition_fingerprint=FINGERPRINT,
        )
    assert stored is not None and stored.profile_row_id == retry_id
    assert first_id is not None and retry_id != first_id


async def test_get_visibility_before_after_commit(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    query = {
        "profile_id": "web",
        "profile_version": "v1",
        "condition_fingerprint": FINGERPRINT,
        "categories": "configuration,auth",
    }
    with TestClient(create_app()) as client:
        client.post("/api/session").raise_for_status()
        async with database.transaction() as db:
            await _register(db, session_id)
            before = client.get(f"/api/sessions/{session_id}/coverage", params=query)
            assert before.status_code == 409
            assert before.json()["code"] == "profile_not_registered"
        after = client.get(f"/api/sessions/{session_id}/coverage", params=query)
        assert after.status_code == 200
        assert after.json()["mode"] == "checklist"


@pytest.mark.parametrize("same", [False, True], ids=["conflict", "same"])
async def test_legacy_empty_hash(database, unique_name, same):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        db.add(
            CoverageProfileRow(
                session_id=context.toolbox.session_id,
                profile_id="web",
                profile_version="v1",
                condition_fingerprint=FINGERPRINT,
                definition_hash="",
                categories=list(PROFILE.check_categories),
                include_parameter_cells=True,
                include_anonymous=True,
            )
        )
    requested = PROFILE if same else replace(PROFILE, check_categories=("configuration",))
    with TestClient(create_app()) as client:
        client.post("/api/session").raise_for_status()
        response = client.post(
            f"/api/sessions/{context.toolbox.session_id}/coverage/profile",
            json=_definition(requested),
        )
    assert response.status_code == (200 if same else 409), response.json()
    async with database.session() as db:
        stored = await CoverageService(db).get_profile(
            context.toolbox.session_id,
            profile_id="web",
            profile_version="v1",
            condition_fingerprint=FINGERPRINT,
        )
    assert stored is not None and stored.categories == list(PROFILE.check_categories)
    assert stored.definition_hash == (profile_definition_hash(PROFILE) if same else "")


@pytest.mark.parametrize(
    "profile",
    [
        replace(PROFILE, check_categories=("auth", "configuration")),
        replace(PROFILE, include_parameter_cells=False),
        replace(PROFILE, include_anonymous=False),
    ],
    ids=["category-order", "parameter-flag", "anonymous-flag"],
)
async def test_definition_changes_are_conflicts(database, unique_name, profile):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        await _register(db, context.toolbox.session_id)
    assert profile_definition_hash(profile) != profile_definition_hash(PROFILE)
    with TestClient(create_app()) as client:
        client.post("/api/session").raise_for_status()
        response = client.post(
            f"/api/sessions/{context.toolbox.session_id}/coverage/profile",
            json=_definition(profile),
        )
    assert response.status_code == 409
    assert "coverage_profile_conflict" in response.json()["message"]


async def test_idempotency_keeps_callers_transaction(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        await _register(db, context.toolbox.session_id)
    async with database.transaction() as db:
        session = await db.get(SessionRow, context.toolbox.session_id)
        assert session is not None
        session.title = "保留同一事务的其他修改"
        await db.flush()
        await _register(db, context.toolbox.session_id)
    async with database.session() as db:
        session = await db.get(SessionRow, context.toolbox.session_id)
        assert session is not None and session.title == "保留同一事务的其他修改"


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--worker":
        raise SystemExit("仅用于隔离测试子进程")
    _worker(sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5])
