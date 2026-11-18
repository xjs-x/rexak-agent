// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api, type AgentEvent, type Snapshot } from "../api/client";
import { visibleAssistantDraft } from "./assistantStream";
import { useWorkbench } from "./useWorkbench";

vi.mock("../api/client", () => ({
  ApiRequestError: Error,
  api: {
    health: vi.fn(async () => true), signIn: vi.fn(async () => {}),
    projects: vi.fn(async () => ({ projects: [] })), allSessions: vi.fn(async () => []),
    modelSettings: vi.fn(async () => null), skills: vi.fn(async () => ({ total: 0 })),
    executionEnvironment: vi.fn(async () => ({ executor_id: "fixture" })),
    snapshot: vi.fn(), eventPage: vi.fn(),
    plan: vi.fn(async () => null), usage: vi.fn(async () => null),
    toolActivities: vi.fn(async () => ({ activities: [], next_offset: null })),
    leads: vi.fn(async () => ({ leads: [] })), weaknesses: vi.fn(async () => ({ weaknesses: [] })),
    conversation: vi.fn(async () => ({ entries: [], next_cursor: null })),
  },
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

test("Main recovers a split private prefix, streams text and resyncs sequence gaps", async () => {
  const streams: Stream[] = [];
  class Stream {
    static CLOSED = 2;
    onopen?: () => void;
    onerror?: () => void;
    listeners = new Map<string, (raw: MessageEvent) => void>();
    constructor(readonly url: string) { streams.push(this); }
    addEventListener(type: string, handler: (event: MessageEvent) => void) { this.listeners.set(type, handler); }
    close = vi.fn();
    emit(event: AgentEvent) { this.listeners.get(event.type)?.(new MessageEvent("message", { data: JSON.stringify(event) })); }
  }
  vi.stubGlobal("EventSource", Stream);
  window.history.replaceState(null, "", "/?session=fixture");
  const event = (seq: number, type: AgentEvent["type"], payload: Record<string, unknown>): AgentEvent => ({
    schema_version: 1, event_id: String(seq), session_seq: seq, type, session_id: "fixture", run_id: "run",
    occurred_at: "2026-11-18T01:00:00Z", payload,
  });
  const delta = (seq: number, text: string, response_id = "reply") =>
    event(seq, "message.delta", { role: "assistant", response_id, delta: text });
  const prefix = delta(1, "<thi");
  const hidden = delta(2, "nk>PRIVATE");
  let history = [hidden];
  let status = "running";
  vi.mocked(api.eventPage).mockImplementation(async (_session, before) => before === 2
    ? { events: [prefix], next_before: null } : { events: history, next_before: 2 });
  vi.mocked(api.snapshot).mockImplementation(async () => ({
    session: { id: "fixture" }, runs: [{ id: "run", status }], findings: [], event_cursor: history.at(-1)!.session_seq,
  } as unknown as Snapshot));

  const { result } = renderHook(useWorkbench);
  const visible = () => visibleAssistantDraft(result.current.assistantDraft);
  await waitFor(() => expect(streams).toHaveLength(1));
  const first = streams[0]!;
  expect(first.url).toContain("after=2");
  expect(visible()).toBe("");
  act(() => first.onopen?.());
  expect(result.current.connection).toBe("online");

  const closeTag = delta(3, "</think>普通");
  const suffix = delta(4, "回复");
  act(() => {
    first.emit(closeTag);
    first.emit(suffix);
    first.emit(closeTag);
  });
  expect(visible()).toBe("普通回复");

  status = "waiting_tool";
  const tool = event(5, "tool.accepted", {});
  act(() => first.emit(tool));
  expect(result.current.lastActivityAt).toBe(tool.occurred_at);
  await waitFor(() => expect(result.current.run?.status).toBe("waiting_tool"));
  expect(visible()).toBe("普通回复");
  act(() => first.onerror?.());
  expect(result.current.connection).toBe("offline");

  const toolDone = event(6, "tool.completed", {});
  const next = delta(7, "Let me think.", "second");
  history = [hidden, closeTag, suffix, tool, toolDone, next];
  act(() => first.emit(next));
  await waitFor(() => expect(streams).toHaveLength(2));
  expect(first.close).toHaveBeenCalledOnce();
  expect(streams[1]?.url).toContain("after=7");
  expect(visible()).toBe("Let me think.");
  act(() => first.emit(delta(8, "stale", "stale")));
  expect(visible()).toBe("Let me think.");
});

test("a fresh snapshot clears the waiting state without a new stream event", async () => {
  let streamCount = 0;
  class Stream {
    static CLOSED = 2;
    readonly readyState = 1;
    constructor() { streamCount += 1; }
    addEventListener = vi.fn();
    close = vi.fn();
  }
  vi.stubGlobal("EventSource", Stream);
  window.history.replaceState(null, "", "/?session=fixture");
  let status = "waiting_tool";
  vi.mocked(api.toolActivities).mockClear();
  vi.mocked(api.eventPage).mockResolvedValue({ events: [], next_before: null });
  vi.mocked(api.snapshot).mockImplementation(async () => ({
    session: { id: "fixture" },
    runs: [{ id: "run", status, model_response_saved: true }],
    findings: [],
    event_cursor: 0,
  } as unknown as Snapshot));

  const { result } = renderHook(useWorkbench);
  await waitFor(() => expect(streamCount).toBe(1));
  // Wait for the initial stream-open refresh before changing the server snapshot.
  await waitFor(() => expect(api.toolActivities).toHaveBeenCalledWith("fixture"));
  expect(result.current.run?.status).toBe("waiting_tool");

  status = "completed";
  await waitFor(() => expect(result.current.run?.status).toBe("completed"), { timeout: 5000 });
  expect(streamCount).toBe(1);
  expect(result.current.snapshot?.event_cursor).toBe(0);
});
