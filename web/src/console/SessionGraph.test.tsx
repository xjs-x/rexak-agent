// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import { SessionGraph } from "./SessionGraph";
import { SMALL_GRAPH } from "./graph/graphTestFixtures";
import { buildGraphFlow } from "./graph/graphLayout";
import type { SessionGraphDocument } from "./graph/graphSchema";

vi.mock("./graph/graphLayout", async importOriginal => {
  const module = await importOriginal<typeof import("./graph/graphLayout")>();
  return { ...module, buildGraphFlow: vi.fn(module.buildGraphFlow) };
});

beforeAll(() => {
  class ResizeObserverMock {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.defineProperty(window, "ResizeObserver", { value: ResizeObserverMock, configurable: true });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function response(fixture: SessionGraphDocument = SMALL_GRAPH) {
  return { ok: true, json: async () => JSON.parse(JSON.stringify(fixture)) } as Response;
}

function search(query: string) {
  const input = screen.getByRole("combobox", { name: "搜索节点名称或 ID" });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: query } });
  fireEvent.keyDown(input, { key: "Enter" });
}

function pane() {
  return within(screen.getByRole("complementary", { name: "图谱详情" }));
}

function positions(container: HTMLElement) {
  return [...container.querySelectorAll<HTMLElement>(".react-flow__node")].map(node => [node.dataset.id, node.style.transform]);
}

test("两张画布都接收完整对象，切图共用一次 HTTP 读取", async () => {
  const fetcher = vi.fn(async () => response());
  vi.stubGlobal("fetch", fetcher);
  const view = render(<SessionGraph sessionId="sample" />);
  await screen.findByText("13 节点 · 12 边 · 3 分量");
  expect(positions(view.container)).toHaveLength(13);
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  await screen.findByText("3 节点 · 3 边 · 2 分量");
  expect(positions(view.container)).toHaveLength(3);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("按 ID 或名称选择全图中的节点，打开详情且无需重新布局", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response()));
  const view = render(<SessionGraph sessionId="sample" />);
  await screen.findByText("13 节点 · 12 边 · 3 分量");
  const initial = positions(view.container);
  vi.mocked(buildGraphFlow).mockClear();
  search("isolated");
  expect(pane().getByRole("heading", { name: "独立节点" })).toBeTruthy();
  search("实体 A");
  expect(pane().getByRole("heading", { name: "实体 A" })).toBeTruthy();
  expect(positions(view.container)).toEqual(initial);
  expect(buildGraphFlow).not.toHaveBeenCalled();
  expect(pane().getByText("00000000-0000-0000-0000-000000000001")).toBeTruthy();
  fireEvent.click(pane().getByRole("button", { name: /关系 0/ }));
  expect(pane().getByRole("heading", { name: "关系 0" })).toBeTruthy();
  fireEvent.click(view.getByTestId("rf__node-neighbor-2"));
  expect(pane().getByRole("heading", { name: "实体 neighbor-2" })).toBeTruthy();
  expect(positions(view.container)).toEqual(initial);
  expect(buildGraphFlow).not.toHaveBeenCalled();
});

test("详情可收起，攻击图详情可跳回其引用实体", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response()));
  render(<SessionGraph sessionId="sample" />);
  await screen.findByText("13 节点 · 12 边 · 3 分量");
  fireEvent.click(screen.getByRole("button", { name: "收起详情" }));
  expect(screen.queryByRole("complementary")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  search("状态一");
  fireEvent.click(pane().getByRole("button", { name: /尝试 0/ }));
  expect(pane().getByRole("heading", { name: "尝试 0" })).toBeTruthy();
  expect(pane().getByText("00000000-0000-0000-0000-000000000001")).toBeTruthy();
  fireEvent.click(pane().getByRole("button", { name: "清除选中" }));
  fireEvent.click(pane().getByRole("button", { name: "实体 A" }));
  expect(screen.getByRole("tab", { name: "信息关系图" }).getAttribute("aria-selected")).toBe("true");
  expect(pane().getByRole("heading", { name: "实体 A" })).toBeTruthy();
});

test("刷新时保留可用画布与选择，失败时保留原 revision 并允许重试", async () => {
  let finish!: (value: Response) => void;
  const fetcher = vi.fn()
    .mockResolvedValueOnce(response())
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ ok: false, status: 500 })
    .mockResolvedValueOnce(response({ ...SMALL_GRAPH, revision: 19 }));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<SessionGraph sessionId="sample" />);
  await screen.findByText("版本 v17");
  search("isolated");
  const canvas = view.container.querySelector(".gflow");
  fireEvent.click(screen.getByRole("button", { name: "刷新图谱" }));
  expect(view.container.querySelector(".gflow")).toBe(canvas);
  await act(async () => finish(response({ ...SMALL_GRAPH, revision: 18 })));
  expect(screen.getByText("版本 v18")).toBeTruthy();
  expect(pane().getByRole("heading", { name: "独立节点" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "刷新图谱" }));
  expect((await screen.findByRole("alert")).textContent).toContain("HTTP 500");
  expect(screen.getByText("版本 v18")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("版本 v19");
});

test("切换 Session 后取消旧请求，忽略仍然完成的旧响应", async () => {
  let finish!: (value: Response) => void;
  const fetcher = vi.fn()
    .mockResolvedValueOnce(response())
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce(response({ ...SMALL_GRAPH, revision: 21 }));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<SessionGraph sessionId="first" />);
  await screen.findByText("版本 v17");
  search("isolated");
  fireEvent.click(screen.getByRole("button", { name: "刷新图谱" }));
  view.rerender(<SessionGraph sessionId="second" />);
  await screen.findByText("版本 v21");
  expect(fetcher.mock.calls[1]?.[1]?.signal.aborted).toBe(true);
  await act(async () => finish(response({ ...SMALL_GRAPH, revision: 99 })));
  expect(screen.queryByText("版本 v99")).toBeNull();
  expect(pane().queryByRole("heading", { name: "独立节点" })).toBeNull();
});

test("空图保留版本，非法引用有清晰提示，概览仍来自文档", async () => {
  const document = structuredClone(SMALL_GRAPH);
  document.attack_graph = { nodes: [], edges: [] };
  document.information_graph.edges.push({ ...document.information_graph.edges[0]!, id: "bad", to_id: "absent" });
  vi.stubGlobal("fetch", vi.fn(async () => response(document)));
  render(<SessionGraph sessionId="sample" />);
  await screen.findByText(/1 条缺少端点的边/);
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  expect(screen.getByText(/暂无节点/)).toBeTruthy();
  expect(screen.getByText("版本 v17")).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  expect(screen.getByText("关系状态分布")).toBeTruthy();
});

test("切图后保留选中对象和浏览历史，概览可直达具体尝试", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response()));
  render(<SessionGraph sessionId="sample" />);
  await screen.findByText("版本 v17");
  search("isolated");
  search("实体 A");
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  search("state-2");
  fireEvent.click(screen.getByRole("tab", { name: "信息关系图" }));
  expect(pane().getByRole("heading", { name: "实体 A" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "后退" }));
  expect(pane().getByRole("heading", { name: "独立节点" })).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  fireEvent.click(screen.getByText("尝试结果无法确认"));
  fireEvent.click(screen.getByRole("button", { name: "尝试 2" }));
  expect(pane().getByRole("heading", { name: "尝试 2" })).toBeTruthy();
});

test("两图都为空且攻击图被关闭时仍显示 revision", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({ revision: 4, information_graph: { nodes: [], edges: [] } }),
  })));
  render(<SessionGraph sessionId="sample" />);
  await screen.findByText("版本 v4");
  expect((screen.getByRole("tab", { name: "攻击状态图" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/暂无节点/)).toBeTruthy();
});
