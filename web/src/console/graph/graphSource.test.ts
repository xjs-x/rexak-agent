// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { GRAPH_ENDPOINT, GraphPayloadError, loadSessionGraph, normalizeGraphPayload } from "./graphSource";

afterEach(() => {
  vi.unstubAllGlobals();
});

const response = (body: unknown, status = 200) =>
  ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }) as unknown as Response;

const validPayload = () => ({
  revision: 3,
  information_graph: {
    nodes: [
      { id: "e1", label: "入口", source_refs: ["operation:11111111-1111-4111-8111-111111111111"] },
      { id: "e2", label: "对象" },
    ],
    edges: [
      {
        id: "r1",
        from_id: "e1",
        to_id: "e2",
        relation: "关联",
        status: "observed",
        source_refs: [],
      },
    ],
  },
  attack_graph: {
    nodes: [{ id: "s1", summary: "入口已观察", entity_refs: ["e1"] }],
    edges: [
      {
        id: "a1",
        from_id: "s1",
        to_id: "s1",
        action: "重试",
        outcome: "unknown",
        source_refs: ["operation:11111111-1111-4111-8111-111111111111"],
      },
    ],
  },
});

test("接口路径就是后端将来要提供的那个只读端点", () => {
  expect(GRAPH_ENDPOINT("abc")).toBe("/api/sessions/abc/graph");
});

test("合法返回被规整成文档，缺失的可选数组按空处理", () => {
  const document = normalizeGraphPayload(validPayload());
  expect(document.revision).toBe(3);
  expect(document.information_graph.nodes).toHaveLength(2);
  expect(document.information_graph.nodes[1]?.source_refs).toEqual([]);
  expect(document.attack_graph.edges[0]?.outcome).toBe("unknown");
});

test("结构不符时抛错并指明路径，而不是猜字段", () => {
  const broken = validPayload() as Record<string, unknown>;
  const information = broken.information_graph as Record<string, unknown>;
  information.edges = [{ id: "r1", from_id: "e1", to_id: "e2", relation: "关联" }];
  expect(() => normalizeGraphPayload(broken)).toThrow(GraphPayloadError);
  try {
    normalizeGraphPayload(broken);
  } catch (cause) {
    expect(String(cause)).toContain("$.information_graph.edges[0].status");
  }
});

test("枚举取值不认识时报错，不做兜底", () => {
  const broken = validPayload();
  broken.information_graph.edges[0]!.status = "guessed" as never;
  expect(() => normalizeGraphPayload(broken)).toThrow(/status/);
});

test("没有选中会话时用演示数据，并且不发起请求", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const source = await loadSessionGraph(null);
  expect(fetchMock).not.toHaveBeenCalled();
  expect(source.status).toBe("demo");
  if (source.status === "demo") expect(source.reason).toContain("尚未选择会话");
});

test("真实会话 HTTP 404 显示错误，不展示样例图", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({}, 404)));
  const source = await loadSessionGraph("session-1");
  expect(source.status).toBe("error");
});

test("真实会话 HTTP 501 显示错误，不展示样例图", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({}, 501)));
  const source = await loadSessionGraph("session-1");
  expect(source.status).toBe("error");
});

test("200 且结构合法时用接口数据，不再混入演示数据", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response(validPayload())));
  const source = await loadSessionGraph("session-1");
  expect(source.status).toBe("remote");
  if (source.status === "remote") {
    expect(source.document.information_graph.nodes).toHaveLength(2);
    expect(source.document.revision).toBe(3);
  }
});

test("两张图都为空时是空态，不是错误", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      response({
        revision: 0,
        information_graph: { nodes: [], edges: [] },
        attack_graph: { nodes: [], edges: [] },
      }),
    ),
  );
  const source = await loadSessionGraph("session-1");
  expect(source.status).toBe("empty");
  if (source.status === "empty") expect(source.reason).toContain("还没有登记");
});

test("网络失败是错误态，给出可重试的提示", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }),
  );
  const source = await loadSessionGraph("session-1");
  expect(source.status).toBe("error");
  if (source.status === "error") expect(source.message).toContain("无法连接");
});

test("HTTP 500 与结构不符都归到错误态，区别在提示文案", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({}, 500)));
  const serverError = await loadSessionGraph("session-1");
  expect(serverError.status).toBe("error");
  if (serverError.status === "error") expect(serverError.message).toContain("HTTP 500");

  vi.stubGlobal("fetch", vi.fn(async () => response({ revision: 1 }, 200)));
  const shapeError = await loadSessionGraph("session-1");
  expect(shapeError.status).toBe("error");
  if (shapeError.status === "error") expect(shapeError.message).toContain("information_graph");
});
