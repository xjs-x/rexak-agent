// @vitest-environment jsdom
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { DeleteSessionDialog, RenameDialog } from "./Dialogs";
import type { SessionItem } from "../api/client";

afterEach(cleanup);
const chat: SessionItem = { id: "chat", project_id: null, title: "独立聊天", status: "active",
  provider_id: "xiaomi", model_name: "mimo-v2.6-pro", created_at: "2026-10-28T00:00:00Z" };
function sidebar() {
  const callbacks = { onSelectSession: vi.fn(), onNewChat: vi.fn(), onNewProject: vi.fn(), onSearch: vi.fn(),
    onCollapse: vi.fn(), onOpenSkills: vi.fn(), onOpenSettings: vi.fn(), onDeleteSession: vi.fn(),
    onRenameSession: vi.fn(), onRenameProject: vi.fn(), onRefreshProject: vi.fn(), onLoadProject: vi.fn() };
  const view = render(<Sidebar projects={[{ id: "project", name: "我的工作台", target_description: "",
    created_at: "2026-10-28T00:00:00Z", sessions: [{ ...chat, id: "grouped", project_id: "project", title: "项目聊天" }] }]}
    standalone={[chat]} sessionId="" skillCount={null} executorState="unknown" executorId={null}
    isProjectLoaded={() => true} {...callbacks} />);
  return { ...view, callbacks };
}

test("普通新聊天无项目，项目按钮只创建该项目的聊天", () => {
  const view = sidebar();
  fireEvent.click(view.getByText("新对话", { exact: true }));
  expect(view.callbacks.onNewChat).toHaveBeenLastCalledWith();
  fireEvent.click(view.getByTitle("在「我的工作台」下新建对话"));
  expect(view.callbacks.onNewChat).toHaveBeenLastCalledWith("project");
  expect(view.getByText("独立聊天")).toBeTruthy();
  expect(view.getByText("项目聊天")).toBeTruthy();
});

test("右键菜单有重命名和删除，点击前不执行操作", () => {
  const view = sidebar();
  fireEvent.contextMenu(view.getByText("独立聊天"), { clientX: 120, clientY: 100 });
  expect(view.getByRole("menu")).toBeTruthy();
  expect(view.callbacks.onDeleteSession).not.toHaveBeenCalled();
  fireEvent.click(view.getByRole("menuitem", { name: "重命名" }));
  expect(view.callbacks.onRenameSession).toHaveBeenCalledWith(chat);
  fireEvent.click(view.getByLabelText("对话操作 独立聊天"));
  fireEvent.click(view.getByRole("menuitem", { name: "删除" }));
  expect(view.callbacks.onDeleteSession).toHaveBeenCalledWith(chat);
});

test("项目菜单可重命名，Escape 关闭菜单", () => {
  const view = sidebar();
  fireEvent.contextMenu(view.getByText("我的工作台"));
  fireEvent.click(view.getByRole("button", { name: "重命名" }));
  expect(view.callbacks.onRenameProject).toHaveBeenCalled();
  fireEvent.contextMenu(view.getByText("项目聊天"));
  fireEvent.keyDown(window, { key: "Escape" });
  expect(view.queryByRole("menu")).toBeNull();
});

test("重命名失败保留输入，删除需确认且取消不删除", async () => {
  const save = vi.fn().mockRejectedValue(new Error("保存失败"));
  const rename = render(<RenameDialog title="重命名对话" initialName="原名称" onSave={save} onClose={vi.fn()} />);
  fireEvent.change(rename.getByLabelText("名称"), { target: { value: "新名称" } });
  fireEvent.click(rename.getByText("保存"));
  await waitFor(() => expect(rename.getByText("保存失败")).toBeTruthy());
  expect((rename.getByLabelText("名称") as HTMLInputElement).value).toBe("新名称");
  rename.unmount();
  const remove = vi.fn(); const close = vi.fn();
  const dialog = render(<DeleteSessionDialog target={chat} onConfirm={remove} onClose={close} />);
  expect(remove).not.toHaveBeenCalled();
  fireEvent.click(dialog.getByText("保留"));
  expect(close).toHaveBeenCalled();
  expect(remove).not.toHaveBeenCalled();
});
