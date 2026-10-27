"""标准 frontmatter 与可选运行提示；导入不执行代码。"""

from __future__ import annotations

import re
from typing import Any

import yaml


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: UniqueLoader, node: Any, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("skill_package_invalid: YAML 键重复或不是字符串")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_skill(files: dict[str, bytes]) -> dict[str, Any]:
    text = files["SKILL.md"].decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)\Z", text, re.S)
    if not match:
        raise ValueError("skill_package_invalid: SKILL.md 缺少 YAML frontmatter")
    if len(match[1]) > 16384:
        raise ValueError("skill_package_invalid: frontmatter 过大")
    meta = yaml.load(match[1], Loader=UniqueLoader)
    if not isinstance(meta, dict):
        raise ValueError("skill_package_invalid: frontmatter 必须是映射")
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("skill_package_invalid: name 需为小写英文、数字、单连字符，最多 64 字符")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise ValueError("skill_package_invalid: description 需为 1–1024 字符")
    for key in ("license", "compatibility"):
        if key in meta and (not isinstance(meta[key], str) or len(meta[key]) > 1024):
            raise ValueError(f"skill_package_invalid: {key} 必须为最多 1024 字符的文本")
    if not match[2].strip():
        raise ValueError("skill_package_invalid: 技能正文为空")
    metadata = meta.get("metadata") or {}
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise ValueError("skill_package_invalid: metadata 必须为字符串映射")
    diagnostics = [f"产品专用字段尚未支持：{key}" for key in meta
                   if key not in {"name", "description", "license", "compatibility", "metadata"}]
    runtime: dict[str, Any] = {}
    if "rexak.yaml" in files:
        runtime = yaml.load(files["rexak.yaml"].decode("utf-8"), Loader=UniqueLoader)
        if not isinstance(runtime, dict) or runtime.get("schema_version") != 1:
            raise ValueError("skill_extension_unsupported: rexak.yaml 只支持 schema_version=1")
        if set(runtime) - {"schema_version", "requires", "entrypoints"}:
            raise ValueError("skill_extension_unsupported: 未知运行字段")
        requires = runtime.get("requires") or {}
        if not isinstance(requires, dict) or set(requires) - {"binaries", "capabilities"}:
            raise ValueError("skill_extension_unsupported: 未知依赖字段")
        for values in requires.values():
            if not isinstance(values, list) or len(values) > 64 or any(not isinstance(x, str) or not x or len(x) > 128 for x in values):
                raise ValueError("skill_package_invalid: 依赖必须为字符串列表")
        entries = runtime.get("entrypoints") or {}
        if not isinstance(entries, dict):
            raise ValueError("skill_package_invalid: entrypoints 必须为映射")
        for entry in entries.values():
            if not isinstance(entry, dict) or set(entry) - {"path", "interpreter"}:
                raise ValueError("skill_extension_unsupported: 未知入口字段")
            if (not isinstance(entry.get("path"), str) or not entry["path"].startswith("scripts/")
                or entry["path"] not in files or entry.get("interpreter") not in {"python", "shell", "node"}):
                raise ValueError("skill_package_invalid: 入口文件或解释器无效")
    return {"name": name, "description": description.strip(), "body": match[2],
            "label": str(metadata.get("version", "")), "diagnostics": diagnostics,
            "compatibility": str(meta.get("compatibility", "")), "runtime": runtime}
