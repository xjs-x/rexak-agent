import { expect, test } from "vitest";
import type { AgentEvent, ConversationEntry } from "../api/client";
import { assistantDisplayText } from "./assistantDisplay";
import { appendAssistantDelta, assistantDelta, isArchivedDraft, restoreAssistantDraft, visibleAssistantDraft, type AssistantDraft } from "./assistantStream";

const startedAt = "2026-11-18T01:00:00Z";
const completedAt = "2026-11-18T01:01:00Z";

function event(delta: string, payload: Record<string, unknown> = {}): AgentEvent {
  return {
    schema_version: 1, event_id: "event", session_seq: 1, session_id: "session", run_id: "run",
    type: "message.delta", occurred_at: startedAt,
    payload: { role: "assistant", response_id: "response", delta, ...payload },
  };
}

test("projects only assistant text and resets assembly on a new response", () => {
  let draft: AssistantDraft | null = null;
  const events = [
    event("<thi"),
    event("PRIVATE_REASONING", { channel: "reasoning" }),
    event("nk>" + "x".repeat(22000) + "PRIVATE_TAGGED"),
    event("</think>Let me think."),
    event("普通回复", { response_id: "next-response" }),
  ];
  const visible = events.map(item => {
    const delta = assistantDelta(item);
    if (delta) draft = appendAssistantDelta(draft, delta);
    return assistantDisplayText(draft?.raw ?? "");
  });
  expect(visible).toEqual(["", "", "", "Let me think.", "普通回复"]);
  expect(assistantDelta(event("hidden", { type: "thinking" }))).toBeNull();
  expect(assistantDelta(event("hidden", { response_id: "host_context_update:1" }))).toBeNull();
  expect(assistantDelta(event("", { event: "final_response_saved" }))).toBeNull();
});

test("only a matching readable archive replaces a live response", () => {
  const draft = assistantDelta(event("正在输出普通回复"))!;
  const entry: ConversationEntry = {
    id: "archive", role: "assistant", run_id: "run", source: "assistant",
    occurred_at: completedAt, preview: "正在输出普通回复", has_more: false, available: true,
  };
  const replaces = (value: ConversationEntry, callId?: string) =>
    isArchivedDraft(draft, draft.raw, [value], callId);
  expect(replaces(entry, "archive")).toBe(true);
  expect(replaces(entry, "other")).toBe(false);
  expect(replaces({ ...entry, available: false })).toBe(false);
  expect(replaces({ ...entry, preview: "<think>hidden</think>" })).toBe(false);
  expect(replaces({ ...entry, preview: "相邻回合的正文" })).toBe(false);
  expect(replaces(entry)).toBe(true);
});

test("retained history without the reply prefix waits for a new response", async () => {
  const tail = { ...event("PRIVATE_ONLY_TAIL"), session_seq: 152 };
  const draft = await restoreAssistantDraft(
    { id: "run", created_at: startedAt }, { events: [tail] },
    async () => { throw new Error("unexpected additional page"); }, () => false,
  );
  expect(visibleAssistantDraft(draft)).toBe("");
  const sameReply = appendAssistantDelta(draft, assistantDelta(event("</think>answer"))!);
  expect(visibleAssistantDraft(sameReply)).toBe("");
  const nextReply = appendAssistantDelta(sameReply, assistantDelta(event("新回合", { response_id: "next" }))!);
  expect(visibleAssistantDraft(nextReply)).toBe("新回合");
});
