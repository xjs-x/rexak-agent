"""台账故障时的持久观察队列；对账只写台账，绝不重新请求模型。"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ModelCallRepository
from agent_workbench.persistence.tables import RunRow


def write_pending_observation(data_root: Path, record: dict[str, Any]) -> None:
    from agent_workbench.maintenance.diagnostics import _safe

    identifier = UUID(record["model_call_id"])
    directory = data_root / "model-observations" / "pending"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{identifier}.json"
    temporary = directory / f".{identifier}.tmp"
    try:
        temporary.write_text(
            json.dumps(_safe(record), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def read_pending_observations(data_root: Path, run_ids: set[str]) -> list[dict[str, Any]]:
    records = []
    for path in sorted((data_root / "model-observations" / "pending").glob("*.json")):
        record = json.loads(path.read_bytes())
        if record.get("run_id") in run_ids:
            records.append(record)
    return records


async def sync_model_observations(database: Database) -> dict[str, Any]:
    from agent_workbench.models.invocation import persist_manifest

    synced = 0
    failed = []
    for path in sorted(
        (database.settings.data_root / "model-observations" / "pending").glob("*.json")
    ):
        try:
            record = json.loads(path.read_bytes())
            identifier = UUID(record["model_call_id"])
            if path.stem != str(identifier) or record["phase"] not in {
                "agent",
                "compaction",
                "memory_distill",
            }:
                raise ValueError("本地观察身份不合法")
            async with database.transaction() as db:
                run = await db.get(RunRow, UUID(record["run_id"]))
                if run is None or str(run.session_id) != record["session_id"]:
                    raise ValueError("本地观察与运行会话不一致")
                repository = ModelCallRepository(db)
                row = await repository.get(identifier)
                if row is None:
                    row = await repository.begin_attempt(
                        model_call_id=identifier,
                        logical_call_id=UUID(record["logical_call_id"]),
                        run_id=run.id,
                        session_id=run.session_id,
                        phase=record["phase"],
                        retry_index=record["retry_index"],
                        provider_id=record["provider_id"],
                        model_name=record["model_name"],
                        attempts_observable=record["attempts_observable"],
                        price_snapshot=record["price_snapshot"],
                    )
                    row.submitted_at = datetime.fromisoformat(record["submitted_at"])
                    if record.get("manifest"):
                        from types import SimpleNamespace

                        toolbox = SimpleNamespace(
                            db=database, run_id=run.id, session_id=run.session_id
                        )
                        await persist_manifest(
                            toolbox,
                            db,
                            identifier,
                            UUID(record["logical_call_id"]),
                            record["phase"],
                            record["manifest"],
                            status="recovered_from_local_observation",
                        )
                if row.completed_at is None or row.pending_sync:
                    await repository.reconcile(
                        model_call_id=identifier,
                        usage_status=record["usage_status"],
                        cost_status=record["cost_status"],
                        input_tokens=record["input_tokens"],
                        output_tokens=record["output_tokens"],
                        cache_read_tokens=record["cache_read_tokens"],
                        cache_write_tokens=record["cache_write_tokens"],
                        reasoning_tokens=record["reasoning_tokens"],
                        raw_usage=record["raw_usage"],
                        cost_amount=Decimal(record["cost_amount"])
                        if record["cost_amount"] is not None
                        else None,
                        cost_currency=record["cost_currency"],
                        completed_at=datetime.fromisoformat(record["completed_at"]),
                        first_token_at=datetime.fromisoformat(record["first_token_at"])
                        if record["first_token_at"]
                        else None,
                        error_code=record["error_code"],
                    )
                    row.pending_sync = False
            path.unlink()
            synced += 1
        except Exception as exc:
            failed.append({"file": path.name, "error_code": type(exc).__name__})
    return {"synced": synced, "failed": failed}
