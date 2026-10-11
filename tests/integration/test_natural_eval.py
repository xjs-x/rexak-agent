from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, cast

import pytest
from evals.acceptance import run_natural_trial
from evals.scenarios import ACCEPTANCE_SCENARIOS
from sqlalchemy import select

from agent_workbench.persistence.tables import IdentityRow, RunRow, SessionRow

pytestmark = pytest.mark.integration


async def test_natural_trials_create_fresh_projects_and_remove_credentials(
    database, tmp_path, monkeypatch
):
    from evals import runner

    original_env = {name for name in os.environ if name.startswith("EVAL_IDENTITY_")}
    generations = []
    projects, sessions = [], []

    class Control:
        async def request(self, method, path):
            if path == "/reset":
                generations.append(str(len(generations) + 1))
            result = {
                "version": "acceptance-fixture-v1",
                "fingerprint": hashlib.sha256(
                    Path("evals/fixtures/acceptance_target.py").read_bytes()
                ).hexdigest(),
                "generation": generations[-1],
                "audit": [],
                "active_requests": 0,
                "canary_hits": 0,
            }
            if path == "/reset":
                result["accounts"] = {
                    "alice": {"username": "alice", "password": "ephemeral-private-password"}
                }
            return result

    async def drive(agent, run_id):
        async with database.transaction() as session:
            run = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
            task = (
                await session.execute(select(SessionRow).where(SessionRow.id == run.session_id))
            ).scalar_one()
            identities = (
                (
                    await session.execute(
                        select(IdentityRow).where(IdentityRow.project_id == task.project_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(identities) == 1
            assert identities[0].secret_ref.startswith("env:EVAL_IDENTITY_")
            assert (
                "ephemeral-private-password" in os.environ[identities[0].secret_ref.split(":")[1]]
            )
            assert "password" not in run.objective
            assert "http_request" not in run.objective
            projects.append(task.project_id)
            sessions.append(task.id)
            run.status = "completed"

    monkeypatch.setattr(runner, "_drive_until_terminal", drive)
    scenario = next(s for s in ACCEPTANCE_SCENARIOS if s.scenario_id == "E02")
    for trial in (1, 2):
        result = await run_natural_trial(scenario, trial, database, cast(Any, Control()), tmp_path)
        assert result["outcome"] == "failed"  # completed 自述不能代替目标证据。
        assert not result["stop_suite"]
        raw = tmp_path / "E02" / f"trial-{trial}"
        assert "ephemeral-private-password" not in (raw / "control.json").read_text(
            encoding="utf-8"
        )
        assert {name for name in os.environ if name.startswith("EVAL_IDENTITY_")} == original_env
    assert len(set(projects)) == 2
    assert len(set(sessions)) == 2
