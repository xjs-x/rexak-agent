// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, type Attachment } from "../api/client";
import { AttachmentCards, useAttachments } from "./Attachments";
import { Composer } from "./ChatView";

vi.mock("../api/client", () => ({ api: {
  temporaryAttachments: vi.fn(), uploadAttachment: vi.fn(), removeAttachment: vi.fn(), attachmentText: vi.fn(),
  attachmentCapabilities: vi.fn(), attachmentAnalyses: vi.fn(),
} }));

const attachment = (id = "attachment-1"): Attachment => ({ attachment_id: id, session_id: "session-1",
  source_message_id: null, filename: "assets.txt", content_type: "text/plain", size: 100,
  sha256: "a".repeat(64), status: "temporary", source: "user_upload", parse_status: "ready", readable: true,
  created_at: "2026-10-27T00:00:00Z", expires_at: "2026-10-28T00:00:00Z", metadata: {} });

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.temporaryAttachments).mockResolvedValue({ attachments: [] });
  vi.mocked(api.removeAttachment).mockResolvedValue({});
  vi.mocked(api.attachmentCapabilities).mockResolvedValue({ max_bytes: 20 * 1024 * 1024, binary_max_bytes: 64 * 1024 * 1024,
    max_per_message: 5, binary_formats: ["PE", "ELF"], binary_analysis: true });
  vi.mocked(api.attachmentAnalyses).mockResolvedValue([]);
});
afterEach(cleanup);

test("二进制上限来自服务端，超过普通上限仍可上传，超二进制上限明确失败", async () => {
  vi.mocked(api.attachmentCapabilities).mockResolvedValue({ max_bytes: 1024, binary_max_bytes: 32 * 1024 * 1024,
    max_per_message: 5, binary_formats: ["PE", "ELF"], binary_analysis: true });
  vi.mocked(api.uploadAttachment).mockResolvedValue({ ...attachment(), filename: "client.exe", readable: false,
    parse_status: "unsupported", metadata: { binary_candidate: true }, size: 27 * 1024 * 1024 });
  const view = composer();
  await waitFor(() => expect(view.getByLabelText("添加文件").getAttribute("title"))
    .toContain("普通附件 ≤ 1.0 KiB；真实 PE/ELF ≤ 32.0 MiB"));
  const file = new File(["fixture"], "client.exe");
  Object.defineProperty(file, "size", { value: 27 * 1024 * 1024 });
  fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [file] } });
  await waitFor(() => expect(api.uploadAttachment).toHaveBeenCalledTimes(1));
  await view.findByText(/已保存/);
  const huge = new File(["fixture"], "huge.exe");
  Object.defineProperty(huge, "size", { value: 33 * 1024 * 1024 });
  fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [huge] } });
  await view.findByText(/文件超过上传上限 32.0 MiB/);
  expect(api.uploadAttachment).toHaveBeenCalledTimes(1);
});

test("二进制未分析与已分析状态来自接口，摘要保持文本转义", async () => {
  const binary = { ...attachment(), filename: "client.exe", readable: false, parse_status: "unsupported",
    metadata: { binary_candidate: true } };
  const view = render(<AttachmentCards sessionId="session-1" attachments={[binary]} />);
  fireEvent.click(view.getByText("client.exe"));
  await view.findByText("未分析；Agent 可按需调用二进制静态分析工具。");
  fireEvent.click(view.getByText("关闭"));
  vi.mocked(api.attachmentAnalyses).mockResolvedValue([{ analysis_id: "analysis-1", kind: "base", status: "ready",
    analyzer_version: "test-version", summary: { format: "PE32+", note: "<script>alert(1)</script>" } }]);
  fireEvent.click(view.getByText("client.exe"));
  await view.findByText(/base · 已分析 · analysis-1/);
  expect(view.container.querySelector("script")).toBeNull();
  expect(api.attachmentText).not.toHaveBeenCalled();
});

function composer(onSend = vi.fn().mockResolvedValue(true), attachmentsEnabled = true) {
  return { onSend, ...render(<Composer attachmentsEnabled={attachmentsEnabled} sessionId="session-1" placeholder="分析文件" scopeMode={null}
    models={[]} modelKey="test" modelLocked={false} onModelChange={() => {}}
    onConfigureModel={() => {}} onScope={() => {}} onToast={() => {}}
    onSend={onSend} />) };
}

test("旧版后端明确禁用附件入口，原文字消息仍然可发送", async () => {
  const view = composer(undefined, false);
  fireEvent.click(view.getByLabelText("添加文件"));
  expect((view.getByText("添加文件").closest("button") as HTMLButtonElement).disabled).toBe(true);
  view.getByText("当前后端尚未加载附件版本；服务切换完成后开放上传。");
  expect(api.temporaryAttachments).not.toHaveBeenCalled();
  fireEvent.change(view.getByLabelText("输入指令"), { target: { value: "普通文字消息" } });
  fireEvent.click(view.container.querySelector('button[type="submit"]')!);
  await waitFor(() => expect(view.onSend).toHaveBeenCalledTimes(1));
  expect(api.uploadAttachment).not.toHaveBeenCalled();
});

test("选择文件后等待真实上传完成，发送仅包含附件 ID，重复点击只发送一次", async () => {
  let complete!: (value: Attachment) => void;
  vi.mocked(api.uploadAttachment).mockImplementation((_sid, _file, _key, _signal, progress) => {
    progress(37);
    return new Promise((resolve) => { complete = resolve; });
  });
  let sent!: (ok: boolean) => void;
  const onSend = vi.fn(() => new Promise<boolean>((resolve) => { sent = resolve; }));
  const view = composer(onSend);
  const file = new File(["should never be copied into message"], "assets.txt", { type: "text/plain" });
  fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [file] } });
  await view.findByText("上传 37%");
  const submit = view.container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  expect(submit.disabled).toBe(true);
  expect(onSend).not.toHaveBeenCalled();
  await act(async () => complete(attachment()));
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit); fireEvent.click(submit);
  await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
  expect(onSend.mock.calls[0]?.slice(0, 3)).toEqual(["请分析附件。", ["attachment-1"], "session-1"]);
  await act(async () => sent(true));
  expect(view.queryByText("assets.txt")).toBeNull();
});

test("拖拽上传失败能重试，并复用上传身份；发送前可移除临时附件", async () => {
  vi.mocked(api.uploadAttachment).mockRejectedValueOnce(new Error("network disconnected"))
    .mockResolvedValueOnce(attachment());
  const view = composer();
  fireEvent.drop(view.container.querySelector("form")!, { dataTransfer: {
    files: [new File(["data"], "assets.txt", { type: "text/plain" })] } });
  await view.findByText("network disconnected");
  fireEvent.click(view.getByText("重试上传"));
  await view.findByText(/已保存/);
  const calls = vi.mocked(api.uploadAttachment).mock.calls;
  expect(calls[0]?.[2]).toBe(calls[1]?.[2]);
  fireEvent.click(view.getByText("移除"));
  await waitFor(() => expect(api.removeAttachment).toHaveBeenCalledWith("session-1", "attachment-1"));
  await waitFor(() => expect(view.queryByText("assets.txt")).toBeNull());
});

test("切换 Session 会中止上传，异步完成不会进入另一个 Session", async () => {
  let signal!: AbortSignal;
  let finish!: (value: Attachment) => void;
  vi.mocked(api.uploadAttachment).mockImplementation((_sid, _file, _key, value) => {
    signal = value;
    return new Promise((resolve) => { finish = resolve; });
  });
  function Harness({ sid }: { sid: string }) {
    const queue = useAttachments(sid);
    return <><button onClick={() => queue.add([new File(["test"], "old.txt")])}>add</button>
      <span>{queue.files.map((f) => f.name).join(",")}</span></>;
  }
  const view = render(<Harness sid="first" />);
  fireEvent.click(view.getByText("add"));
  await waitFor(() => expect(signal).toBeDefined());
  view.rerender(<Harness sid="second" />);
  expect(signal.aborted).toBe(true);
  await act(async () => finish(attachment()));
  expect(view.queryByText("old.txt")).toBeNull();
});

test("刷新后恢复未绑定的已上传附件；历史附件正文始终以转义文本预览", async () => {
  vi.mocked(api.temporaryAttachments).mockResolvedValue({ attachments: [attachment()] });
  const view = composer();
  await view.findByText("assets.txt");
  expect(api.uploadAttachment).not.toHaveBeenCalled();
  view.unmount();
  vi.mocked(api.attachmentText).mockResolvedValue({ attachment_id: "attachment-1", reference: "attachment:attachment-1",
    status: "ok", source: "user_upload_untrusted", text: "<script>alert('xss')</script>", page: 1, start_line: 1, end_line: 1,
    char_offset: 0, note: "user data" });
  const history = render(<AttachmentCards sessionId="session-1" attachments={[attachment()]} />);
  fireEvent.click(history.getByText("assets.txt"));
  await history.findByText("<script>alert('xss')</script>");
  expect(history.container.querySelector("script")).toBeNull();
  expect(history.getByText("下载原文件").getAttribute("href")).toContain("/attachments/attachment-1/content");
});
