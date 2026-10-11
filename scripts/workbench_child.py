"""启动现有 CLI，并让 Windows Ctrl+Break 走正常退出清理。"""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from types import FrameType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_workbench.cli import main


def interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, interrupt)
    raise SystemExit(main())
