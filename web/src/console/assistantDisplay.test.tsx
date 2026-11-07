// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { assistantDisplayText } from "./assistantDisplay";
import { MarkdownText } from "./Markdown";

afterEach(cleanup);

test.each([
  ["<think>hidden analysis</think>正常答复", "正常答复"],
  [" \n<THINK>hidden\n</THINK>\n# 结果", "# 结果"],
  ["<think>unfinished", ""],
  ["<think>first</think> <think>second</think>answer", "answer"],
  ["plain <think>example</think>", "plain <think>example</think>"],
  ["```xml\n<think>example</think>\n```", "```xml\n<think>example</think>\n```"],
])("projects assistant text without changing quoted examples: %s", (raw, visible) => {
  expect(assistantDisplayText(raw)).toBe(visible);
});

test("every streaming prefix keeps private thinking out of the DOM", () => {
  const raw = "<think>PRIVATE_REASONING_SENTINEL</think>**正常答复**";
  for (let end = 0; end <= raw.length; end += 1) {
    const view = render(<MarkdownText text={assistantDisplayText(raw.slice(0, end))} />);
    expect(view.container.textContent).not.toContain("PRIVATE");
    expect(view.container.textContent).not.toContain("<think");
    view.unmount();
  }
  const view = render(<MarkdownText text={assistantDisplayText(raw)} />);
  expect(view.container.querySelector("strong")?.textContent).toBe("正常答复");
});
