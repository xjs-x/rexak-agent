"""PTY 的行边界与 UTF-8 字节边界都不能跳过尚未返回的数据。"""

import asyncio
from types import SimpleNamespace

from workbench_executor.settings import ExecutorSettings
from workbench_executor.terminals import TerminalRuntime


def test_line_limit_advances_only_returned_bytes(tmp_path):
    text = "第一行\n第二行\n第三行\n"
    (tmp_path / "stdout.log").write_bytes(text.encode())

    async def get_terminal(_terminal_id):
        return {"log_dir": str(tmp_path), "state": "running"}

    runtime = TerminalRuntime(ExecutorSettings(token="fixture", data_root=tmp_path),
                              SimpleNamespace(get_terminal=get_terminal), "fixture")

    async def scenario():
        first = await runtime.read_page("terminal", cursor=None, stream="stdout", limit=4096,
                                        max_lines=1)
        second = await runtime.read_page("terminal", cursor=first.next_cursor,
                                         stream="stdout", limit=4096, max_lines=1)
        assert first.text == "第一行\n"
        assert first.byte_count == len(first.text.encode())
        assert not first.at_current_end
        assert second.text == "第二行\n"
        small = await runtime.read_page("terminal", cursor=None, stream="stdout", limit=1)
        assert small.text and small.byte_count > 0

    asyncio.run(scenario())
