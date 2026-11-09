"""Only the display projection hides provider thinking, including partial output."""

import pytest

from agent_workbench.api.assistant_display import assistant_display_text


@pytest.mark.parametrize(("raw", "visible"), [
    ("<think>private analysis</think>正常答复", "正常答复"),
    ("<think>hidden</think></think>正常答复", "正常答复"),
    ("</THINK> </think>正常答复", "正常答复"),
    ("</thi", ""),
    ("普通正文 </think>示例", "普通正文 </think>示例"),
    (" \n<THINK>hidden\n" + "x" * 1400 + "</THINK>\n# 结果\n完成", "# 结果\n完成"),
    ("<think>unfinished", ""),
    ("<thi", ""),
    ("<think", ""),
    ("<think>one</think> <think>two</think>answer", "answer"),
    ("<think></think>", ""),
    ("普通正文 <think>示例</think>", "普通正文 <think>示例</think>"),
    ("```xml\n<think>example</think>\n```", "```xml\n<think>example</think>\n```"),
    ("`<think>` 是一个标记", "`<think>` 是一个标记"),
    ("unchanged", "unchanged"),
])
def test_display_projection(raw, visible):
    assert assistant_display_text(raw) == visible
