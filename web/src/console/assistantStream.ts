import type { AgentEvent, ConversationEntry, RunItem } from "../api/client";
import { assistantDisplayText, isInternalAssistantContent } from "./assistantDisplay";

export type AssistantDraft = {
  runId: string;
  responseId: string | null;
  startedAt: string;
  raw: string;
  prefixUnavailable?: boolean;
};

export function assistantDelta(event: AgentEvent) {
  if (event.type !== "message.delta" || !event.run_id) return null;
  const payload = event.payload ?? {};
  const responseId = typeof payload.response_id === "string" ? payload.response_id : null;
  if ((payload.role != null && payload.role !== "assistant") || isInternalAssistantContent(payload) ||
      /^host_(context_update|history_reference):/.test(responseId ?? "") ||
      typeof payload.delta !== "string" || !payload.delta) return null;
  return { runId: event.run_id, responseId, startedAt: event.occurred_at, raw: payload.delta };
}

function sameResponse(left: AssistantDraft, right: AssistantDraft) {
  return left.runId === right.runId && left.responseId === right.responseId;
}

export function appendAssistantDelta(previous: AssistantDraft | null, delta: AssistantDraft): AssistantDraft {
  return previous && sameResponse(previous, delta) ? { ...previous, raw: previous.raw + delta.raw } : delta;
}

type ReplayPage = { events: AgentEvent[]; next_before?: number | null };

export async function restoreAssistantDraft(
  run: Pick<RunItem, "id" | "created_at">,
  firstPage: ReplayPage,
  readBefore: (cursor: number) => Promise<ReplayPage>,
  isClosed: () => boolean,
): Promise<AssistantDraft | null> {
  let page = firstPage;
  let draft: AssistantDraft | null = null;
  const chunks: string[] = [];
  const runCreatedAt = Date.parse(run.created_at);
  const build = (prefixUnavailable = false) =>
    draft && { ...draft, raw: chunks.reverse().join(""), prefixUnavailable };

  // Recover the start before projecting text. A recent page can begin inside a <think> block.
  while (!isClosed()) {
    for (const event of [...page.events].reverse()) {
      if (Date.parse(event.occurred_at) < runCreatedAt) return build();
      const delta = assistantDelta(event);
      if (!delta || delta.runId !== run.id) continue;
      if (draft && !sameResponse(draft, delta)) return build();
      draft = delta;
      chunks.push(delta.raw);
    }
    if (page.next_before == null) return build((page.events[0]?.session_seq ?? 1) > 1);
    page = await readBefore(page.next_before);
  }
  return null;
}

export function visibleAssistantDraft(draft: AssistantDraft | null) {
  return draft && !draft.prefixUnavailable ? assistantDisplayText(draft.raw) : "";
}

export function isArchivedDraft(
  draft: AssistantDraft, visible: string, entries: ConversationEntry[], modelCallId?: string,
) {
  return entries.some(entry => {
    if (entry.role !== "assistant" || entry.run_id !== draft.runId || !entry.available ||
        isInternalAssistantContent(entry)) return false;
    const preview = assistantDisplayText(entry.preview).trim();
    if (!preview) return false;
    if (modelCallId && entry.source === "assistant") return entry.id === modelCallId;
    // Older events may have no response-to-archive ID mapping.
    const draftPrefix = visible.trim();
    return Date.parse(entry.occurred_at) >= Date.parse(draft.startedAt) &&
      (draftPrefix.startsWith(preview) || preview.startsWith(draftPrefix));
  });
}
