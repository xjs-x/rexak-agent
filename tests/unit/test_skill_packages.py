"""包格式、路径和原件一致性；不调用模型或目标。"""

import io
import zipfile

import pytest

from agent_workbench.contracts.skill_package import (
    decode_package,
    encode_package,
    package_hash,
    safe_skill_path,
)
from agent_workbench.skills.parser import parse_skill


def sample(body="固定的技能指导"):
    return {"SKILL.md": f"---\nname: fixture-skill\ndescription: 中文参数采集方法\n---\n{body}\n".encode(),
            "references/detail.md": "参考资料".encode(), "scripts/main.py": b"print('fixture')\n"}


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "C:/x", "a\\x", "a//x", "a/./x", "NUL", "a/con.txt", "a/../x", "a./x"])
def test_unsafe_paths_rejected(path):
    with pytest.raises(ValueError, match="skill_path_denied"):
        safe_skill_path(path)


def test_whole_package_hash_and_roundtrip():
    files = sample()
    assert decode_package(encode_package(files)) == files
    assert package_hash(dict(reversed(list(files.items())))) == package_hash(files)
    changed = {**files, "references/detail.md": "新的资料".encode()}
    assert package_hash(changed) != package_hash(files)
    assert parse_skill(files)["description"] == "中文参数采集方法"


def test_archive_root_and_case_collision():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, body in sample().items():
            archive.writestr("fixture-skill/" + name, body)
    assert decode_package(data.getvalue(), allow_root=True) == sample()
    with pytest.raises(ValueError, match="大小写"):
        encode_package({**sample(), "skill.md": b"collision"})


@pytest.mark.parametrize("change", [b"\xef\xbb\xbf---\n", b"\xff\xfe", b"---\nname: X\ndescription: x\n---\nx",
    b"---\nname: fixture-skill\nname: duplicate\ndescription: x\n---\nx"])
def test_invalid_text_or_metadata(change):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        parse_skill(decode_package(encode_package({"SKILL.md": change})))


def test_product_extensions_are_diagnostic_not_authorization():
    files = sample()
    files["SKILL.md"] = files["SKILL.md"].replace(b"description:", b"mcps: [unknown]\nallowed-tools: Bash\ndescription:")
    result = parse_skill(files)
    assert len(result["diagnostics"]) == 2


def test_resource_manifest_paginates_and_unload_removes_checkpoint_reference():
    from agent_workbench.agent.state import merge_skill_refs
    from agent_workbench.skills.service import SkillService

    files = {**sample(), **{f"assets/file-{index:03}.bin": b"fixture" for index in range(100)}}
    first = SkillService.resource(files, "SKILL.md")
    assert first.files_total == 103 and len(first.files) == 50 and first.next_file_offset == 50
    second = SkillService.resource(files, "SKILL.md", files_offset=50)
    assert not set(first.files) & set(second.files)
    third = SkillService.resource(files, "SKILL.md", files_offset=100)
    assert len(third.files) == 3 and third.next_file_offset is None
    assert merge_skill_refs({"old": {"version_id": "v1"}}, {"old": None}) == {}


def test_skill_script_does_not_bypass_command_tool_selection():
    from agent_workbench.tools.registry import ToolSelection, build_tools

    selection = ToolSelection(command=False, skills=True)
    names = {item.name for item in build_tools(selection)}
    assert "run_skill_script" not in names
    assert {"search_skills", "load_skill", "read_skill_resource", "unload_skill"} <= names
    assert "run_skill_script" in selection.disabled_tools()
