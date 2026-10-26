"""生产配方合同夹具：只用 MockTransport，不连接执行岛或模型。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from experiments.history_action_trusted_shadow_v1.fixture import (
    make_toolbox,
    parsed,
    tool_arguments,
)

from agent_workbench.domain.history_action_reflection import digest


def policy_for(box, profiles=None):
    profiles = profiles or {
        "baseline": {"request": parsed(tool_arguments()).model_dump(exclude={"note"})}
    }
    return {
        "session_id": str(box.session_id),
        "run_id": str(box.run_id),
        "task_version": "autonomous-task/1",
        "revision": 1,
        "status": "active",
        "valid_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "scope_revision": box.scope_revision,
        "scope_sha256": digest(box.scope.snapshot.model_dump(mode="json")),
        "policy_revision": box.policy_revision,
        "control_version": box.control_version,
        "executor_id": box.executor._preflight.executor_id,
        "executor_boot_id": box.executor._preflight.boot_id,
        "http_enabled": True,
        "profiles": {
            key: {
                "environment": "fixture-environment/1",
                "configuration": "fixture-configuration/1",
                "prerequisites": {"controller_fixture": "ready/1"},
                **value,
            }
            for key, value in profiles.items()
        },
    }


def save_policy(box, value):
    box.settings.history_action_shadow_context_path.write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


def make_box(root, enabled=True, profiles=None):
    box = make_toolbox(root, enabled, [])
    box.settings.history_action_shadow_grants_path = None
    box.settings.history_action_shadow_context_path = root / "task-context.json"
    save_policy(box, policy_for(box, profiles))
    return box
