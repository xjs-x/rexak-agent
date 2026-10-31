// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import { SessionGraph } from "./SessionGraph";
import { MOCK_SESSION_GRAPH } from "./graph/graphMock";
import { WING_LIMIT } from "./graph/graphLayout";
import {
  ATTEMPT_OUTCOME_HINTS,
  ATTEMPT_OUTCOME_LABELS,
  FIELD_LABELS,
  RELATION_STATUS_HINTS,
  RELATION_STATUS_LABELS,
} from "./graph/graphLabels";

/**
 * jsdom 没有 ResizeObserver，React Flow 用它测量节点与会话宽度。
 * 这里只提供空实现：节点仍会渲染，只是视口适配拿不到真实尺寸。
 * 连线是否绘制由 graphLayout.test.ts 直接断言布局结果（jsdom 画不出 SVG 路径）。
 */
beforeAll(() => {
  class NoopResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.defineProperty(globalThis, "ResizeObserver", {
    writable: true,
    value: NoopResizeObserver,
  });
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const INFORMATION = MOCK_SESSION_GRAPH.information_graph;
const ATTACK = MOCK_SESSION_GRAPH.attack_graph;
const WEB = INFORMATION.nodes[0]!;
const STATE = ATTACK.nodes[0]!;

function renderGraph() {
  return render(<SessionGraph />);
}

function sideOf(view: ReturnType<typeof renderGraph>) {
  const side = view.container.querySelector(".gside") as HTMLElement | null;
  expect(side).toBeTruthy();
  return within(side as HTMLElement);
}

function openPicker(view: ReturnType<typeof renderGraph>) {
  const button = view.container.querySelector(".gpicker-btn");
  expect(button).toBeTruthy();
  fireEvent.click(button as HTMLElement);
  const pop = view.container.querySelector(".gpicker-pop");
  expect(pop).toBeTruthy();
  return pop as HTMLElement;
}

function navButtons(view: ReturnType<typeof renderGraph>) {
  const back = view.container.querySelector('.gnav button[aria-label="后退"]') as HTMLButtonElement;
  const forward = view.container.querySelector('.gnav button[aria-label="前进"]') as HTMLButtonElement;
  expect(back).toBeTruthy();
  expect(forward).toBeTruthy();
  return { back, forward };
}

function currentFocus(view: ReturnType<typeof renderGraph>) {
  return view.container.querySelector(".gpicker-btn")?.textContent ?? "";
}

function pickFocus(view: ReturnType<typeof renderGraph>, name: string) {
  openPicker(view);
  const item = [...view.container.querySelectorAll(".gpicker-item")].find((node) =>
    (node.textContent ?? "").includes(name),
  );
  expect(item, `下拉里没有：${name}`).toBeTruthy();
  fireEvent.click(item as HTMLElement);
}

test("默认以第一个实体为焦点，画布只画它的一跳邻居", () => {
  const view = renderGraph();
  expect(screen.getByRole("tab", { name: "信息关系图" }).getAttribute("aria-selected")).toBe("true");

  const button = view.container.querySelector(".gpicker-btn");
  expect(button?.textContent).toContain(WEB.label);

  const onCanvas = [...view.container.querySelectorAll(".react-flow__node")];
  expect(onCanvas.length).toBeGreaterThan(1);
  // 两跳以外的对象不上画布。
  expect(onCanvas.some((node) => (node.textContent ?? "").includes("数据库连接池配置"))).toBe(false);
});

test("焦点节点与两翼在视觉上分得开", () => {
  const view = renderGraph();
  const focusNode = view.container.querySelector(".gnode.role-focus");
  expect(focusNode).toBeTruthy();
  expect(focusNode?.querySelector(".gnode-focus")?.textContent).toBe("当前");
  expect(view.container.querySelectorAll(".gnode.role-outbound").length).toBeGreaterThan(0);
});

test("焦点选择器列出全部对象，并支持搜索过滤", () => {
  const view = renderGraph();
  const pop = openPicker(view);
  const items = pop.querySelectorAll(".gpicker-item");
  expect(items.length).toBe(INFORMATION.nodes.length);
  expect(pop.textContent).toContain(`共 ${INFORMATION.nodes.length} 个`);

  const search = pop.querySelector(".gpicker-search") as HTMLInputElement;
  fireEvent.change(search, { target: { value: "admin" } });
  const filtered = [...pop.querySelectorAll(".gpicker-item")];
  expect(filtered.length).toBeLessThan(items.length);
  expect(filtered.length).toBeGreaterThan(0);
  for (const item of filtered) {
    expect((item.textContent ?? "").toLowerCase()).toContain("admin");
  }
});

test("从下拉里选一个对象，画布与详情一起换", () => {
  const view = renderGraph();
  pickFocus(view, "管理后台审计日志");

  const button = view.container.querySelector(".gpicker-btn");
  expect(button?.textContent).toContain("管理后台审计日志");

  const panel = sideOf(view);
  expect(panel.getByText(FIELD_LABELS.label)).toBeTruthy();
  expect(panel.getByText("管理后台审计日志 /admin/audit", { selector: ".gv" })).toBeTruthy();
});

test("单侧邻居超过上限时出现折叠节点，点击后列出被收起来的对象", () => {
  const view = renderGraph();
  const folds = view.container.querySelectorAll(".gnode-fold");
  expect(folds.length).toBeGreaterThan(0);

  const outbound = INFORMATION.edges.filter(
    (edge) => edge.from_id === WEB.id && edge.to_id !== WEB.id,
  );
  expect(outbound.length).toBeGreaterThan(WING_LIMIT);

  const outboundFold = [...folds].find((node) => node.classList.contains("outbound"));
  expect(outboundFold).toBeTruthy();
  expect(outboundFold?.textContent).toContain(`还有 ${outbound.length - WING_LIMIT} 个`);

  fireEvent.click(outboundFold as HTMLElement);
  const panel = sideOf(view);
  expect(panel.getByText("折叠的去向", { selector: ".gkind" })).toBeTruthy();
  expect(panel.getByText(/它还指向更多对象/)).toBeTruthy();
  expect(panel.getByText(new RegExp(`这一侧共 ${outbound.length} 个`))).toBeTruthy();
});

test("详情面板按统一字段映射展示焦点实体的真实字段", () => {
  const view = renderGraph();
  pickFocus(view, "登录接口");
  const panel = sideOf(view);

  expect(panel.getByText("实体", { selector: ".gkind" })).toBeTruthy();
  const keys = [...view.container.querySelectorAll(".gside .gk")].map((node) => node.textContent);
  expect(keys).toContain(FIELD_LABELS.label);
  expect(keys).toContain(FIELD_LABELS.id);
  expect(panel.getByText(new RegExp(`^${FIELD_LABELS.source_refs}（\\d+）$`))).toBeTruthy();
  for (const raw of ["label", "source_refs", "entity_refs", "from_id", "to_id"]) {
    expect(keys).not.toContain(raw);
  }

  expect(panel.getAllByText("工具操作").length).toBeGreaterThan(0);
  expect(panel.getByText(/相关关系（\d+）/)).toBeTruthy();
});

test("缺证据实体在画布与详情里都显示空状态，不伪造来源", () => {
  const view = renderGraph();
  pickFocus(view, "数据库连接池配置");
  const panel = sideOf(view);
  expect(panel.getByText(/尚无证据来源/)).toBeTruthy();
  expect(panel.queryAllByText("工具操作").length).toBe(0);
  expect(view.getAllByText("尚无证据来源").length).toBeGreaterThan(0);
});

test("点击相关关系后选中该连线，详情切换成关系字段，并能退回焦点", () => {
  const view = renderGraph();
  pickFocus(view, "会话 Cookie 策略");
  const panel = sideOf(view);

  const target = panel
    .getAllByRole("button")
    .find((button) => button.className === "grel-btn" && button.textContent?.includes("假设"));
  expect(target).toBeTruthy();
  fireEvent.click(target as HTMLElement);

  const keys = [...view.container.querySelectorAll(".gside .gk")].map((node) => node.textContent);
  expect(keys).toContain(FIELD_LABELS.relation);
  expect(keys).toContain(FIELD_LABELS.status);
  expect(keys).toContain(FIELD_LABELS.from_id);
  expect(keys).toContain(FIELD_LABELS.to_id);
  expect(panel.getAllByText(RELATION_STATUS_LABELS.hypothesis).length).toBeGreaterThan(0);
  expect(panel.getByText(RELATION_STATUS_HINTS.hypothesis)).toBeTruthy();

  fireEvent.click(panel.getByText("返回焦点"));
  expect(panel.getByText(FIELD_LABELS.label)).toBeTruthy();
});

test("切换到攻击状态图后只渲染状态，不复制信息图的实体", () => {
  const view = renderGraph();
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));

  expect(view.container.querySelectorAll(".react-flow__node .gnode-entity").length).toBe(0);
  expect(view.container.querySelectorAll(".gnode-state").length).toBeGreaterThan(0);
  expect(view.getAllByText(STATE.summary).length).toBeGreaterThan(0);
});

test("攻击状态详情把关联实体与相关尝试分开列，并说明证据挂在尝试上", () => {
  const view = renderGraph();
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  const panel = sideOf(view);

  expect(panel.getByText(FIELD_LABELS.summary)).toBeTruthy();
  expect(panel.getByText(/关联实体（\d+）/)).toBeTruthy();
  expect(panel.getByText(/相关尝试（\d+）/)).toBeTruthy();
  expect(panel.getByText(/攻击状态本身不携带证据来源/)).toBeTruthy();
});

test("尝试详情保留 unknown 的不确定性，不写成漏洞成立", () => {
  const view = renderGraph();
  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  pickFocus(view, "已持有测试账户会话");
  const panel = sideOf(view);

  const target = panel
    .getAllByRole("button")
    .find((button) => (button.textContent ?? "").includes("以账户 A 的会话请求账户 B 的用户资源"));
  expect(target).toBeTruthy();
  fireEvent.click(target as HTMLElement);

  const keys = [...view.container.querySelectorAll(".gside .gk")].map((node) => node.textContent);
  expect(keys).toContain(FIELD_LABELS.action);
  expect(keys).toContain(FIELD_LABELS.outcome);
  expect(panel.getByText(ATTEMPT_OUTCOME_LABELS.unknown)).toBeTruthy();
  expect(panel.getByText(ATTEMPT_OUTCOME_HINTS.unknown)).toBeTruthy();
});

test("图版本与总数取自文档本身", () => {
  const view = renderGraph();
  expect(view.getByText(`${FIELD_LABELS.revision} v${MOCK_SESSION_GRAPH.revision}`)).toBeTruthy();
  expect(
    view.getByText(
      `共 ${INFORMATION.nodes.length} 个实体 · ${INFORMATION.edges.length} 条关联关系`,
    ),
  ).toBeTruthy();

  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  expect(
    view.getByText(`共 ${ATTACK.nodes.length} 个攻击状态 · ${ATTACK.edges.length} 条测试尝试`),
  ).toBeTruthy();
});

test("页面始终标注演示数据，并说明图内容是模型判断", () => {
  const view = renderGraph();
  expect(view.getAllByText("演示数据").length).toBeGreaterThan(0);
  expect(view.getByText(/当前显示演示数据/)).toBeTruthy();
  expect(view.getByText(/尚未选择会话/)).toBeTruthy();
  expect(view.getByText(/model_judgment/)).toBeTruthy();
});

test("浏览历史：初始时后退与前进都不可用", () => {
  const view = renderGraph();
  const { back, forward } = navButtons(view);
  expect(back.disabled).toBe(true);
  expect(forward.disabled).toBe(true);
  expect(currentFocus(view)).toContain(WEB.label);
});

test("浏览历史：连续换焦点后能后退，也能再前进回来", () => {
  const view = renderGraph();
  pickFocus(view, "登录接口");
  pickFocus(view, "会话 Cookie 策略");

  expect(currentFocus(view)).toContain("会话 Cookie 策略");
  expect(navButtons(view).back.disabled).toBe(false);
  expect(navButtons(view).forward.disabled).toBe(true);

  fireEvent.click(navButtons(view).back);
  expect(currentFocus(view)).toContain("登录接口");
  expect(navButtons(view).forward.disabled).toBe(false);

  fireEvent.click(navButtons(view).back);
  expect(currentFocus(view)).toContain(WEB.label);
  expect(navButtons(view).back.disabled).toBe(true);

  fireEvent.click(navButtons(view).forward);
  expect(currentFocus(view)).toContain("登录接口");
});

test("浏览历史：后退之后改选新对象，会截断原来的前进分支", () => {
  const view = renderGraph();
  pickFocus(view, "登录接口");
  pickFocus(view, "会话 Cookie 策略");
  fireEvent.click(navButtons(view).back);
  expect(currentFocus(view)).toContain("登录接口");

  pickFocus(view, "管理后台 /admin");
  expect(currentFocus(view)).toContain("管理后台 /admin");
  // 分支被截断，不能再前进到「会话 Cookie 策略」。
  expect(navButtons(view).forward.disabled).toBe(true);
  fireEvent.click(navButtons(view).back);
  expect(currentFocus(view)).toContain("登录接口");
});

test("浏览历史：Alt + 方向键与按钮等效", () => {
  const view = renderGraph();
  pickFocus(view, "登录接口");

  fireEvent.keyDown(window, { key: "ArrowLeft", altKey: true });
  expect(currentFocus(view)).toContain(WEB.label);

  fireEvent.keyDown(window, { key: "ArrowRight", altKey: true });
  expect(currentFocus(view)).toContain("登录接口");
});

test("浏览历史：切换图会重置，不会带着另一张图的焦点", () => {
  const view = renderGraph();
  pickFocus(view, "登录接口");
  expect(navButtons(view).back.disabled).toBe(false);

  fireEvent.click(screen.getByRole("tab", { name: "攻击状态图" }));
  expect(navButtons(view).back.disabled).toBe(true);
  expect(navButtons(view).forward.disabled).toBe(true);
  expect(currentFocus(view)).toContain(STATE.summary);
});

test("浏览历史：重复选同一个对象不会产生一条空记录", () => {
  const view = renderGraph();
  pickFocus(view, "登录接口");
  // 再选一次同一个：后退仍只回一步到默认焦点。
  pickFocus(view, "登录接口");
  expect(navButtons(view).back.disabled).toBe(false);
  fireEvent.click(navButtons(view).back);
  expect(currentFocus(view)).toContain(WEB.label);
  expect(navButtons(view).back.disabled).toBe(true);
});

test("概览页展示总数与两图分布，数字全部来自文档", () => {
  const view = renderGraph();
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));

  expect(view.getByText("关系状态分布")).toBeTruthy();
  expect(view.getByText("尝试结果分布")).toBeTruthy();
  expect(view.getByText("需要留意")).toBeTruthy();
  // 五张大数字：实体 / 关系 / 状态 / 尝试 / 自环
  const totals = [...view.container.querySelectorAll(".gtotal strong")].map((node) => node.textContent);
  expect(totals).toEqual([
    String(INFORMATION.nodes.length),
    String(INFORMATION.edges.length),
    String(ATTACK.nodes.length),
    String(ATTACK.edges.length),
    String(ATTACK.edges.filter((edge) => edge.from_id === edge.to_id).length),
  ]);
  // 概览不是图，不该渲染画布。
  expect(view.container.querySelector(".react-flow")).toBeNull();
});

test("概览页的「需要留意」可以展开并跳到对应对象", () => {
  const view = renderGraph();
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));

  const summary = view.getByText("实体没有任何证据来源");
  fireEvent.click(summary);
  const links = [...view.container.querySelectorAll(".gissue-list .glink")];
  expect(links.length).toBeGreaterThan(0);

  fireEvent.click(links[0] as HTMLElement);
  // 跳回信息图，并把该实体设成焦点。
  expect(screen.getByRole("tab", { name: "信息关系图" }).getAttribute("aria-selected")).toBe("true");
  expect(view.container.querySelector(".gpicker-btn")?.textContent).toContain(
    (links[0]?.textContent ?? "").slice(0, 4),
  );
});

test("概览页在窄屏下也会渲染，不依赖画布尺寸", () => {
  const view = renderGraph();
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  expect(view.container.querySelector(".goverview")).toBeTruthy();
  expect(view.container.querySelector(".gfocus-bar")).toBeNull();
});

test("真实会话手动刷新读取新版本，切换会话不保留旧图", async () => {
  let revision = 1;
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => ({ ok: true, status: 200,
    json: async () => ({revision: revision++, information_graph: {nodes: [], edges: []}, attack_graph: {nodes: [], edges: []}}) }));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<SessionGraph sessionId="first" />);
  await screen.findByText("这个会话还没有登记任何图对象。");
  fireEvent.click(screen.getByRole("button", {name: "刷新图谱"}));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  view.rerender(<SessionGraph sessionId="second" />);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
  expect(fetcher.mock.calls[2]?.[0]).toBe("/api/sessions/second/graph");
});
