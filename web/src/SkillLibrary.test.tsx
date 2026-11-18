// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ApiRequestError, api, type SkillItem } from "./api/client";
import { Composer } from "./console/ChatView";
import { SkillsDialog } from "./console/Dialogs";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

test("Escape and close preserve drafts until the user explicitly abandons them", async () => {
  vi.spyOn(api, "skills").mockResolvedValue({ skills: [], total: 0, root: "" });
  const close = vi.fn();
  render(<SkillsDialog sessionId="" onClose={close} />);
  fireEvent.click(screen.getByRole("button", { name: "新建技能" }));
  const name = screen.getByRole("textbox", { name: "名称" });
  fireEvent.change(name, { target: { value: "document-review" } });
  fireEvent.keyDown(window, { key: "Escape" });
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));
  expect((name as HTMLInputElement).value).toBe("document-review");
  fireEvent.click(screen.getByRole("button", { name: /^关闭$/ }));
  fireEvent.click(await screen.findByRole("button", { name: "放弃并关闭" }));
  expect(close).toHaveBeenCalledOnce();
});

test("a stale save keeps the old draft beside the newly opened current version", async () => {
  const first: SkillItem = {
    skill_id: "fixture", name: "document-review", description: "Review document headings",
    source: "user:web:document-review", enabled: false, version_id: "v1", package_hash: "old", label: "",
  };
  const latest = { ...first, version_id: "v2", package_hash: "new" };
  vi.spyOn(api, "skills").mockResolvedValue({ skills: [first], total: 1, root: "" });
  vi.spyOn(api, "skillVersions").mockResolvedValue({ skills: [latest, first], total: 2, root: "" });
  vi.spyOn(api, "skillDetail").mockResolvedValue(latest);
  vi.spyOn(api, "skillResource").mockImplementation(async (_id, version) => ({
    path: "SKILL.md", sha256: version, byte_size: 10, files: ["SKILL.md"], files_total: 1,
    text: version === "v1" ? "Previous text" : "Other editor's changes",
  }));
  const save = vi.spyOn(api, "editSkillFiles").mockRejectedValue(new ApiRequestError("版本冲突", 409, {}));
  render(<SkillsDialog sessionId="" onClose={() => {}} />);
  fireEvent.click(await screen.findByRole("button", { name: "查看 document-review 的版本与文件" }));
  const editor = await screen.findByRole("textbox", { name: "编辑 Skill 文件" });
  fireEvent.change(editor, { target: { value: "My unsaved edit" } });
  fireEvent.click(screen.getByRole("button", { name: "保存新版本" }));
  fireEvent.click(await screen.findByRole("button", { name: "加载最新版本" }));
  const previous = await screen.findByRole("textbox", { name: "原草稿" });
  expect((previous as HTMLTextAreaElement).value).toBe("My unsaved edit");
  expect((screen.getByRole("textbox", { name: "编辑 Skill 文件" }) as HTMLTextAreaElement).value).toBe("Other editor's changes");
  expect(save).toHaveBeenCalledOnce();
});

test("an accepted slash command reports an unconfirmed result if the checkpoint never reflects it", async () => {
  vi.useFakeTimers();
  vi.spyOn(api, "sessionSkillCatalog").mockResolvedValue({
    root: "", total: 1,
    skills: [{ skill_id: "fixture", name: "review", description: "Review document headings",
      source: "user:web:review", enabled: true, version_id: "v1", package_hash: "hash", label: "" }],
  });
  const status = vi.spyOn(api, "activeSkills").mockResolvedValue({ active: [], note: "" });
  const send = vi.fn().mockResolvedValue(true);
  render(<Composer attachmentsEnabled={false} sessionId="test-session" placeholder="Type a message" scopeMode={null}
    models={[]} modelKey="" modelLocked={false} onModelChange={() => {}} onConfigureModel={() => {}}
    onScope={() => {}} onToast={() => {}} onSend={send} />);
  const input = screen.getByRole("textbox", { name: "输入指令" });
  await act(async () => fireEvent.change(input, { target: { value: "/rev" } }));
  await act(async () => fireEvent.keyDown(input, { key: "Enter" }));
  expect(send).toHaveBeenCalledOnce();
  expect(send.mock.calls[0]?.[4]).toEqual(["fixture"]);
  expect(screen.getByText(/Skill 装配中/)).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(30000));
  expect(screen.getByText(/装配结果暂未确认/)).toBeTruthy();
  const polls = status.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(30000));
  expect(status.mock.calls).toHaveLength(polls);
});
