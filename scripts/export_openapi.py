"""导出控制端 OpenAPI 供前端生成 TS 类型，不连接数据库。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_workbench.api.app import create_app


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/export_openapi.py web/openapi.json")
    target = Path(sys.argv[1]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"OpenAPI 已导出：{target}")


if __name__ == "__main__":
    main()
