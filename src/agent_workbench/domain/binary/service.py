"""鉴权、短事务租约、跨进程并发保护、CAS 缓存和有界查询。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.binary_analysis import LIMITATIONS, TRUST
from agent_workbench.domain.attachments import load_attachment
from agent_workbench.domain.binary.model import ANALYZER, ANALYZER_VERSION, address_location
from agent_workbench.domain.binary.strings import KINDS
from agent_workbench.domain.isolated import run_isolated
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import AttachmentAnalysisRow, AttachmentRow


@asynccontextmanager
async def analysis_slot(database: Database, count: int):
    """会话级 advisory lock 跨 API/Runner 限流；解析期间不持有数据库事务。"""
    async with database.engine.connect() as connection:
        acquired = None
        try:
            for slot in range(count):
                if await connection.scalar(
                    text("SELECT pg_try_advisory_lock(428231, :slot)"), {"slot": slot}
                ):
                    acquired = slot
                    break
            await connection.commit()
            yield acquired is not None
        finally:
            if acquired is not None:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(428231, :slot)"), {"slot": acquired}
                    )
                    await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise


class BinaryService:
    def __init__(
        self, database: Database, session_id: UUID, settings: Settings, store: ArtifactStore
    ) -> None:
        self.db, self.sid, self.cfg, self.store = database, session_id, settings, store

    def parameters(self, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "max_bytes": self.cfg.binary_attachment_max_bytes,
            "max_strings": self.cfg.binary_analysis_max_strings,
            "output_bytes": self.cfg.binary_analysis_output_bytes,
        }

    async def authorize(self, aid: UUID) -> AttachmentRow:
        async with self.db.session() as db:
            return await load_attachment(db, self.sid, aid, self.cfg, bound_only=True)

    async def index(
        self, attachment: AttachmentRow, kind: str = "base", extra: dict[str, Any] | None = None
    ) -> tuple[Any, Any, bool]:
        params = self.parameters(kind) | (extra or {})
        params_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        identity = {
            "attachment_id": attachment.id,
            "sha256": attachment.sha256,
            "kind": kind,
            "analyzer": ANALYZER,
            "analyzer_version": ANALYZER_VERSION,
            "params_hash": params_hash,
        }
        filters = [getattr(AttachmentAnalysisRow, k) == v for k, v in identity.items()]
        filters += [
            AttachmentAnalysisRow.session_id == self.sid,
            AttachmentAnalysisRow.owner_id == self.cfg.owner_id,
        ]
        if not self.store.exists(attachment.relative_key) or not await asyncio.to_thread(
            self.store.verify, attachment.relative_key, attachment.sha256
        ):
            return None, {"status": "missing", "note": "附件缺失或 SHA256 不符。"}, False
        async with self.db.session() as db:
            row = (
                await db.execute(select(AttachmentAnalysisRow).where(*filters))
            ).scalar_one_or_none()
        if row and row.status in {"ready", "unsupported"} and row.result_key:
            cached = await self.load_index(row)
            if cached is not None:
                return row, cached, True
        async with analysis_slot(self.db, self.cfg.binary_analysis_concurrency) as available:
            if not available:
                return None, {"status": "busy", "note": "静态分析并发已满，请稍后重试。"}, False
            now, attempt = datetime.now(UTC), uuid4()
            async with self.db.transaction() as db:
                await db.execute(
                    insert(AttachmentAnalysisRow)
                    .values(
                        **identity,
                        id=uuid4(),
                        session_id=self.sid,
                        owner_id=self.cfg.owner_id,
                        params_json=params,
                        status="pending",
                        summary_json={},
                    )
                    .on_conflict_do_nothing(constraint="uq_attachment_analysis_cache")
                )
                row = (
                    await db.execute(
                        select(AttachmentAnalysisRow).where(*filters).with_for_update()
                    )
                ).scalar_one()
                if row.status in {"ready", "unsupported"} and row.result_key:
                    cached = await self.load_index(row)
                    if cached is not None:
                        return row, cached, True
                if row.status == "pending" and row.lease_expires_at and row.lease_expires_at > now:
                    return row, {"status": "pending", "note": "同一附件分析正在进行。"}, False
                row.status, row.attempt_id = "pending", attempt
                row.lease_expires_at = now + timedelta(
                    seconds=self.cfg.binary_analysis_seconds + 15
                )
                row.error_type = None
                row.result_key = None
                row.result_sha256 = None
                row.completed_at = None
                row.summary_json = {}
                analysis_id = row.id
            # 等待任务完成时屏蔽取消，确保子进程已回收再释放并发槽。
            task = asyncio.create_task(
                asyncio.to_thread(
                    run_isolated,
                    "agent_workbench.domain.binary.worker",
                    self.store.resolve(attachment.relative_key),
                    [attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else "", json.dumps(params)],
                    tmp_root=self.cfg.tmp_root,
                    seconds=self.cfg.binary_analysis_seconds,
                    memory_mb=self.cfg.binary_analysis_memory_mb,
                    output_bytes=self.cfg.binary_analysis_output_bytes,
                )
            )
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            stored = None
            if result.get("status") in {"ready", "unsupported"}:
                profile = result.get("profile", {})
                if profile.get("sha256") != attachment.sha256:
                    result = {"status": "failed", "error_type": "SourceHashMismatch"}
                elif profile.get("analyzer_version") != ANALYZER_VERSION:
                    result = {"status": "failed", "error_type": "AnalyzerVersionMismatch"}
                else:
                    stored = await asyncio.to_thread(
                        self.store.write_bytes,
                        json.dumps(result, ensure_ascii=False).encode("utf-8"),
                        media_type="application/vnd.rexak.binary-index+json",
                        captured_by="user_attachment_derived",
                    )
            async with self.db.transaction() as db:
                row = await db.get(AttachmentAnalysisRow, analysis_id, with_for_update=True)
                assert row is not None
                if row.attempt_id == attempt:
                    row.status = result.get("status", "failed")
                    row.error_type = result.get("error_type")
                    row.completed_at, row.lease_expires_at = datetime.now(UTC), None
                    if stored:
                        row.result_key, row.result_sha256 = stored.relative_key, stored.sha256
                    p = result.get("profile", {})
                    row.summary_json = (
                        {k: p[k] for k in ("format", "architecture", "bits", "strings") if k in p}
                        if isinstance(p, dict)
                        else {}
                    )
            return row, result, False

    async def load_index(self, row: AttachmentAnalysisRow) -> dict[str, Any] | None:
        if (
            not row.result_key
            or not row.result_sha256
            or not self.store.exists(row.result_key)
            or not await asyncio.to_thread(self.store.verify, row.result_key, row.result_sha256)
        ):
            return None
        try:
            raw = await asyncio.to_thread(
                self.store.read_bytes,
                row.result_key,
                limit=self.cfg.binary_analysis_output_bytes + 1,
            )
            if len(raw) > self.cfg.binary_analysis_output_bytes:
                return None
            return json.loads(raw)
        except (ValueError, OSError, ArtifactError):
            return None  # 无有效缓存时重建，不返回伪造成功。

    async def query(self, aid: UUID, view: str, **args: Any) -> dict[str, Any]:
        if not valid_query(view, args):
            return {
                "status": "invalid_parameters",
                "note": "参数越界；页大小上限为 100（指令页为 120）。",
            }
        attachment = await self.authorize(aid)
        row, index, cached = await self.index(attachment)
        # 完成后再次校验：缓存永远不绕过当前 Session/owner/绑定状态。
        await self.authorize(aid)
        ref = f"binary:{aid}:sha256:{attachment.sha256}"
        envelope = {
            "attachment_id": str(aid),
            "sha256": attachment.sha256,
            "source": TRUST,
            "reference": ref + f"#view={view}",
            "analysis_id": str(row.id) if row else None,
            "cached": cached,
            "analyzer": ANALYZER,
            "analyzer_version": ANALYZER_VERSION,
            "limitations": LIMITATIONS,
        }
        if index["status"] != "ready":
            return {**envelope, **index}
        if view == "profile":
            profile = dict(index["profile"])
            sections = profile["sections"]
            profile["sections"] = sections[:8]
            profile["sections_truncated"] = len(sections) > 8
            return {
                **envelope,
                "status": "ok",
                "profile": profile,
                "index_reference": f"analysis:{row.id}",
            }
        if view == "strings":
            if index["profile"]["strings"]["truncated"] and (
                args.get("query")
                or args.get("kind", "any") != "any"
                or args.get("section") is not None
            ):
                filters = {
                    "query": args.get("query", "").casefold(),
                    "kind": args.get("kind", "any"),
                    "section": args.get("section"),
                    "min_length": args.get("min_length", 6),
                }
                row, index, cached = await self.index(attachment, "strings", {"filters": filters})
                await self.authorize(aid)
                envelope.update(analysis_id=str(row.id) if row else None, cached=cached)
                if index["status"] != "ready":
                    return {**envelope, **index}
            page = query_strings(index, **args)
            for item in page["items"]:
                item["reference"] = ref + f"#file={item['file_offset']}&encoding={item['encoding']}"
                for location in item["locations"]:
                    location["reference"] = (
                        ref + f"#file={location['file_offset']}&encoding={item['encoding']}"
                    )
            return {**envelope, **page}
        code_row, code, code_cached = await self.index(attachment, "code")
        await self.authorize(aid)
        envelope.update(analysis_id=str(code_row.id) if code_row else None, cached=code_cached)
        if code["status"] != "ready":
            return {**envelope, **code}
        if view == "functions":
            return {**envelope, **query_functions(code, **args)}
        if view == "references":
            return {**envelope, **query_references(index, code, **args)}
        if view == "function":
            selector = args["selector"]
            functions = code["functions"]
            hits = [f for f in functions if f["name"] == selector]
            if not hits:
                try:
                    address = int(selector.removeprefix("va:"), 0)
                except ValueError:
                    address = -1
                hits = [f for f in functions if f["start"] == address]
            if len(hits) != 1:
                return {
                    **envelope,
                    "status": "not_found" if not hits else "ambiguous",
                    "note": "未唯一定位索引函数；请从 binary_functions 选择精确起始 VA。",
                }
            function_row, body, cache_hit = await self.index(
                attachment, "function", {"function": hits[0]}
            )
            await self.authorize(aid)
            envelope.update(
                analysis_id=str(function_row.id) if function_row else None,
                cached=cache_hit,
                reference=ref
                + f"#function={hits[0]['start']:#x}&instruction={args.get('offset', 0)}",
            )
            if body["status"] != "ready":
                return {**envelope, **body}
            offset, limit = args.get("offset", 0), args.get("limit", 120)
            instructions = body["instructions"]
            return {
                **envelope,
                "status": "ok",
                "function": hits[0],
                "mode": "disasm",
                "offset": offset,
                "total": len(instructions),
                "remaining": max(0, len(instructions) - offset - limit),
                "items": [
                    dict(zip(("address", "bytes", "mnemonic", "operands"), i, strict=True))
                    for i in instructions[offset : offset + limit]
                ],
                "next_offset": offset + limit if offset + limit < len(instructions) else None,
                "truncated": body["disassembly_truncated"],
                "calls": body["calls"][:8],
                "disassembly_limitations": body["disassembly_limitations"],
            }
        return {**envelope, "status": "unsupported"}


def valid_query(view: str, args: dict[str, Any]) -> bool:
    offset, limit = args.get("offset", 0), args.get("limit", 50)
    if not 0 <= offset <= 500000 or not 1 <= limit <= (120 if view == "function" else 100):
        return False
    if view == "strings":
        return (
            offset <= 300000
            and len(args.get("query", "")) <= 200
            and args.get("kind", "any") in KINDS
            and 4 <= args.get("min_length", 6) <= 512
            and (args.get("section") is None or len(args["section"]) <= 160)
        )
    if view == "functions":
        return (
            len(args.get("name_contains", "")) <= 200
            and 0 <= args.get("min_size", 0) <= 64 * 1024 * 1024
        )
    if view == "function":
        return bool(args.get("selector")) and len(args["selector"]) <= 200
    if view == "references":
        return bool(args.get("target")) and len(args["target"]) <= 512
    return view == "profile"


def query_functions(
    index: dict[str, Any],
    name_contains: str = "",
    min_size: int = 0,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    items = [
        f
        for f in index["functions"]
        if name_contains.casefold() in f["name"].casefold()
        and ((f["size"] is not None and f["size"] >= min_size) or not min_size)
    ]
    result = {
        "status": "ok",
        "total": len(items),
        "offset": offset,
        "items": items[offset : offset + limit],
        "next_offset": offset + limit if offset + limit < len(items) else None,
        "truncated": index["functions_truncated"],
        "complete_function_recovery": False,
        "function_limitations": index["function_limitations"],
    }
    if len(items) > 5000 and not name_contains and not min_size:
        result.update(
            status="too_broad",
            items=[],
            next_offset=None,
            note="请用名称或 min_size 缩小范围。索引不代表全部函数。",
        )
    return result


def query_references(
    base: dict[str, Any], code: dict[str, Any], target: str, offset: int = 0, limit: int = 50
) -> dict[str, Any]:
    if code["references_status"] == "unsupported":
        return {"status": "unsupported", "reference_limitations": code["reference_limitations"]}
    addresses: set[int] = set()
    try:
        prefix, value = target.split(":", 1) if ":" in target else ("va", target)
        number = int(value, 0)
        if prefix == "va":
            addresses.add(number)
        elif prefix == "rva" and base["profile"].get("image_base") is not None:
            addresses.add(base["profile"]["image_base"] + number)
        elif prefix == "file":
            mapped = address_location(number, base["profile"]["sections"])
            if mapped["va"] is not None:
                addresses.add(mapped["va"])
    except ValueError:
        addresses.update(f["start"] for f in code["functions"] if f["name"] == target)
        addresses.update(s[5] for s in base["strings"] if s[1] == target and s[5] is not None)
    rows = [r for r in code["references"] if r[1] in addresses]
    names = {f["start"]: f["name"] for f in code["functions"]}
    return {
        "status": "partial",
        "target_addresses": sorted(addresses)[:20],
        "offset": offset,
        "total": len(rows),
        "items": [
            {
                "instruction": r[0],
                "target": r[1],
                "type": r[2],
                "function_start": r[3],
                "function": names.get(r[3]),
                "file_offset": r[4],
                "method": "linear_sweep",
            }
            for r in rows[offset : offset + limit]
        ],
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "truncated": code["references_truncated"],
        "scan": code["reference_scan"],
        "reference_limitations": code["reference_limitations"],
    }


def query_strings(
    index: dict[str, Any],
    *,
    query: str = "",
    kind: str = "any",
    section: str | None = None,
    min_length: int = 6,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    if (
        len(query) > 200
        or kind not in KINDS
        or not 4 <= min_length <= 512
        or not 0 <= offset <= 300000
        or not 1 <= limit <= 100
        or (section is not None and len(section) > 160)
    ):
        return {"status": "invalid_parameters"}
    stats = index["profile"]["strings"]
    total = sum(
        1
        for s in index["strings"]
        if len(s[1]) >= min_length
        and query.casefold() in s[1].casefold()
        and (kind == "any" or s[6] == kind)
        and (section is None or s[3] == section)
    )
    result: dict[str, Any] = {
        "status": "ok",
        "total": total,
        "offset": offset,
        "statistics": stats,
        "truncated": stats["truncated"],
        "items": [],
        "next_offset": None,
    }
    if total > 5000 and not query and kind == "any" and section is None:
        return {
            **result,
            "status": "too_broad",
            "note": "请指定 query、kind 或 section；total 仅为已索引范围。",
        }
    hits = (
        s
        for s in index["strings"]
        if len(s[1]) >= min_length
        and query.casefold() in s[1].casefold()
        and (kind == "any" or s[6] == kind)
        and (section is None or s[3] == section)
    )
    for i, s in enumerate(hits):
        if i < offset:
            continue
        location = {"file_offset": s[0], "section": s[3], "rva": s[4], "va": s[5]}
        if (
            result["items"]
            and result["items"][-1]["text"] == s[1]
            and result["items"][-1]["encoding"] == s[2]
            and len(result["items"][-1]["locations"]) < 8
        ):
            item = result["items"][-1]
            item["locations"].append(location)
            item["occurrences"] += 1
            item["next_offset"] = i + 1
        else:
            result["items"].append(
                {
                    "file_offset": s[0],
                    "text": s[1],
                    "encoding": s[2],
                    "section": s[3],
                    "rva": s[4],
                    "va": s[5],
                    "kind": s[6],
                    "locations": [location],
                    "occurrences": 1,
                    "next_offset": i + 1,
                }
            )
        if i + 1 >= offset + limit:
            break
    result["next_offset"] = (
        result["items"][-1]["next_offset"]
        if result["items"] and result["items"][-1]["next_offset"] < total
        else None
    )
    return result
