"""技能库维护命令。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from agent_workbench.contracts.skill_package import decode_package, encode_package, package_hash
from agent_workbench.persistence.db import Database
from agent_workbench.skills.parser import parse_skill
from agent_workbench.skills.service import SkillService, read_directory

app = typer.Typer(help="管理标准 SKILL.md 技能包", no_args_is_help=True)


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


async def _service(action: str, value: str = "", version: str | None = None) -> Any:
    database = Database()
    await database.start()
    try:
        service = SkillService(database)
        if action == "list":
            return [item.model_dump() for item in await service.list()]
        if action == "rescan":
            rows, errors = await service.rescan()
            return {"skills": [item.model_dump() for item in rows], "errors": errors, "root": str(service.root)}
        if action == "import":
            path = Path(value)
            if path.is_dir():
                return (await service.import_directory(value)).model_dump()
            files = decode_package(path.read_bytes(), allow_root=True)
            return (await service.publish(files, f"zip:{parse_skill(files)['name']}")).model_dump()
        if action == "export":
            _card, files, _spec = await service.files(value, version, enforce_enabled=False)
            return encode_package(files)
        raise ValueError("未知维护操作")
    finally:
        await database.close()


@app.command("list")
def list_command() -> None:
    _print(asyncio.run(_service("list")))


@app.command("rescan")
def rescan_command() -> None:
    _print(asyncio.run(_service("rescan")))


@app.command("import")
def import_command(path: str) -> None:
    _print(asyncio.run(_service("import", path)))


@app.command("validate")
def validate_command(path: Path) -> None:
    files = read_directory(path) if path.is_dir() else decode_package(path.read_bytes(), allow_root=True)
    spec = parse_skill(files)
    _print({"valid": True, "name": spec["name"], "package_hash": package_hash(files),
            "files": sorted(files), "diagnostics": spec["diagnostics"]})


@app.command("export")
def export_command(skill_id: str, output: Path, version: str | None = None) -> None:
    if output.exists():
        raise typer.BadParameter("输出文件已存在，请选择新路径")
    output.write_bytes(asyncio.run(_service("export", skill_id, version)))
    _print({"exported": str(output)})
