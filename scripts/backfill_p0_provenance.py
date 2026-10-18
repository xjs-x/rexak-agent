"""P0：回填三个可唯一追溯的 Phineas 导出工件来源。

只处理已知、可核对的三条记录；不猜测其他历史工件。
默认 dry-run；使用 `--apply` 才写入。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from agent_workbench.config.settings import get_settings
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import ArtifactProvenanceRow, ArtifactRow
from agent_workbench.platform_compat import configure_event_loop_policy

SESSION_ID = UUID("4f68cc55-963f-41d1-9d15-373fb11d5ba2")
RUN_ID = UUID("f5ea33d0-5760-4ac7-a6d1-bdb85314285e")

ROWS = [
    {
        "artifact_id": UUID("7bb44be3-71bc-5d8b-9c06-b3924d2d0289"),
        "sha256": "97bee522a9e56d8215779cd3ccaf219ea78ebcf49622954cca275b7bc659cea6",
        "byte_size": 861,
        "export_tool_call_id": "call_a8614d6d47fd41c6be1bf639",
        "origin_operation_id": UUID("020c0b50-e35c-4809-98f1-99352b631680"),
        "resource_id": UUID("3d5cb1aa-9599-43ab-8c5a-d191aa0944ac"),
        "captured_at": "2026-10-17T17:26:19.147381+00:00",
    },
    {
        "artifact_id": UUID("4cd24362-8fd6-5b2c-a18d-346cfbc32d5f"),
        "sha256": "dbcb4bcc5f14949607338d56a4d92c5d68f400670c1196084ff10ce172ef09ba",
        "byte_size": 898,
        "export_tool_call_id": "call_5e4c0dbeed014f90ab2e713a",
        "origin_operation_id": UUID("1405f6ea-137d-44ef-a3c5-936e56ede699"),
        "resource_id": UUID("6b82394b-4bd6-4cda-be79-bcd5b9cfd4ec"),
        "captured_at": "2026-10-17T17:29:25.672266+00:00",
    },
    {
        "artifact_id": UUID("d062579b-3957-5902-b711-279a63c59d2d"),
        "sha256": "3d37b776837b2de7f8d2d95dd00359b8697269d059776ebfe25aee82b4448872",
        "byte_size": 855,
        "export_tool_call_id": "call_e17e614ce88e4db499580caa",
        "origin_operation_id": UUID("7784e983-9a09-49e9-affe-8e38ba37c196"),
        "resource_id": UUID("4430ddf5-4960-48a3-bae9-be240650fc7b"),
        "captured_at": "2026-10-17T17:30:15.917252+00:00",
    },
]


def _parse_time(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _run(apply: bool) -> None:
    db = Database(get_settings())
    await db.start()
    try:
        async with db.transaction() as session:
            for item in ROWS:
                artifact = await session.get(ArtifactRow, item["artifact_id"])
                if artifact is None:
                    raise RuntimeError(f"工件不存在：{item['artifact_id']}")
                if artifact.session_id != SESSION_ID:
                    raise RuntimeError(f"工件不属于目标会话：{item['artifact_id']}")
                if artifact.sha256 != item["sha256"] or artifact.byte_size != item["byte_size"]:
                    raise RuntimeError(f"工件哈希/大小不匹配：{item['artifact_id']}")
                existing = (
                    await session.execute(
                        select(ArtifactProvenanceRow).where(
                            ArtifactProvenanceRow.artifact_id == item["artifact_id"],
                            ArtifactProvenanceRow.provenance_revision == 1,
                            ArtifactProvenanceRow.segment_no == 0,
                        )
                    )
                ).scalar_one_or_none()
                print(
                    f"{'APPLY' if apply else 'DRY  '} {item['artifact_id']} "
                    f"origin={item['origin_operation_id']} export={item['export_tool_call_id']}"
                )
                if not apply:
                    continue
                artifact.export_tool_call_id = item["export_tool_call_id"]
                artifact.producer_kind = "execution_output"
                artifact.source_trust = "ledger"
                artifact.resolution_status = "resolved"
                if existing is None:
                    session.add(
                        ArtifactProvenanceRow(
                            provenance_id=uuid4(),
                            artifact_id=item["artifact_id"],
                            provenance_revision=1,
                            segment_no=0,
                            export_tool_call_id=item["export_tool_call_id"],
                            origin_operation_id=item["origin_operation_id"],
                            origin_run_id=RUN_ID,
                            resource_id=item["resource_id"],
                            stream="stdout",
                            artifact_byte_start=0,
                            artifact_byte_end=item["byte_size"],
                            source_byte_start=0,
                            source_byte_end=item["byte_size"],
                            snapshot_hash=item["sha256"],
                            identity_id=None,
                            identity_revision=None,
                            connection_origin={"executor_id": "kali-01"},
                            producer_kind="execution_output",
                            source_trust="ledger",
                            resolution_status="resolved",
                            confidence="receipt_verified",
                            total_stream_completeness="unknown",
                            captured_at=_parse_time(item["captured_at"]),
                        )
                    )
                else:
                    existing.export_tool_call_id = item["export_tool_call_id"]
                    existing.origin_operation_id = item["origin_operation_id"]
                    existing.origin_run_id = RUN_ID
                    existing.resource_id = item["resource_id"]
                    existing.snapshot_hash = item["sha256"]
                    existing.producer_kind = "execution_output"
                    existing.source_trust = "ledger"
                    existing.resolution_status = "resolved"
                    existing.confidence = "receipt_verified"
                    existing.captured_at = _parse_time(item["captured_at"])
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="写入数据库；默认只打印")
    args = parser.parse_args()
    configure_event_loop_policy()
    asyncio.run(_run(args.apply))


if __name__ == "__main__":
    main()
