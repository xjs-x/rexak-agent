"""校验原封存和仅测试类型桥补证；不修改原 freeze，不运行模型或实验。"""

from __future__ import annotations

import ast
import json

from experiments.history_action_reflection_preparation_v1.prepare import REPO, ROOT, sha
from experiments.history_action_reflection_preparation_v1.verify import input_sources, preservation


class StripTypingBridge(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        return None if node.module == "typing" else node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "cast" and len(node.args) == 2:
            return self.visit(node.args[1])
        return self.generic_visit(node)


def verify():
    supplement = json.loads((ROOT / "typing-supplement.json").read_text(encoding="utf-8"))
    if sha(ROOT / "freeze-final.json") != supplement["original_freeze_sha256"]:
        raise ValueError("original_freeze_changed")
    frozen = json.loads((ROOT / "freeze-final.json").read_text(encoding="utf-8"))
    target = "tests/experiments/test_reflection_preparation.py"
    archived = ROOT / "test-source-before-typing/test_reflection_preparation.py"
    for name, digest in frozen["files"].items():
        source = archived if name == target else REPO / name
        if sha(source) != digest:
            raise ValueError("frozen_material_changed:" + name)
    for name, digest in supplement["current_sources"].items():
        if sha(REPO / name) != digest:
            raise ValueError("typed_source_changed:" + name)
    before, after = [ast.dump(StripTypingBridge().visit(ast.parse(p.read_text(encoding="utf-8"))), include_attributes=False)
                     for p in (archived, REPO / target)]
    if before != after:
        raise ValueError("change_exceeds_type_bridge")
    return {"original_freeze_sha256": sha(ROOT / "freeze-final.json"),
            "typing_supplement_sha256": sha(ROOT / "typing-supplement.json"),
            "test_body_equal_after_removing_typing_bridges": True,
            "preservation": preservation(), **input_sources()}


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False))
