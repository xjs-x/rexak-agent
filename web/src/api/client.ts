import type { components } from "./generated/schema";

export type ProjectCreate = components["schemas"]["ProjectCreate"];
export type SessionCreate = components["schemas"]["SessionCreate"];
export type AgentEvent = components["schemas"]["EventEnvelope"];

export interface ProjectItem {
  id: string;
  name: string;
  target_description: string;
  created_at: string;
}

export interface SessionItem {
  id: string;
  project_id: string;
  title: string;
  status: string;
  provider_id: string | null;
  model_name: string | null;
  created_at: string;
}

export interface RunItem {
  id: string;
  status: string;
  completion_reason: string | null;
  objective: string;
  control_version: number;
  stop_reason?: string | null;
  created_at: string;
}

export interface Snapshot {
  session: {
    id: string;
    project_id: string;
    title: string;
    status: string;
    scope?: components["schemas"]["ScopeSnapshot"];
    policy?: unknown;
  };
  runs: RunItem[];
  leads: LeadItem[];
  findings: FindingItem[];
  coverage: {
    total: number;
    by_status: Record<string, number>;
    ratio_definition: string;
  };
  event_cursor: number;
}

export interface FindingItem {
  id: string;
  title: string;
  category: string;
  status: string;
  description?: string;
  impact?: string | null;
  evidence_refs?: string[];
  verification_id: string | null;
}

export interface LeadItem {
  id: string;
  hypothesis: string;
  status: string;
  priority: number;
  merged_into_id?: string | null;
  blocked_reason: string | null;
  next_action?: string | null;
  evidence_refs?: string[];
}

export interface JobItem {
  job_id: string;
  state: string;
  sync_status: string;
  cancel_requested: boolean;
  cancel_confirmed: boolean;
  last_synced_at: string | null;
}

export interface ReportItem {
  version: number;
  status: string;
  is_historical: boolean;
  created_at: string;
}

export interface ModelSettings {
  default_provider: string;
  price_catalog_version: string;
  effective_scope: string;
  profiles: Array<{
    provider_id: string;
    model_name: string;
    secret_configured: boolean;
    context_limit: number;
    profile_version: string;
  }>;
}

export type ModelCallItem = components["schemas"]["ModelCallSummary"];
export type OperationItem = components["schemas"]["OperationSummary"];
export type ToolActivityItem = components["schemas"]["ToolActivitySummary"];
export type ToolActivityDetail = components["schemas"]["ToolActivityDetail"];
export type SpanItem = components["schemas"]["SpanSummary"];
export type ConversationEntry = components["schemas"]["ConversationEntry"];
export type ConversationBody = components["schemas"]["ConversationBody"];
export type PriceCatalog = components["schemas"]["PriceCatalog"];
export type PriceUpdate = components["schemas"]["PriceUpdate"];
export type PlanRead = components["schemas"]["PlanRead"];

export interface UsageSummary {
  call_count: number;
  by_currency: Record<string, { known_cost: string; priced_calls: number }>;
  unpriced_calls: number;
  unknown_usage_calls: number;
  token_totals: Record<string, number | null>;
  note: string;
}

export interface EventPage {
  events: AgentEvent[];
  next_before: number | null;
}

export interface ArtifactPreview {
  artifact_id: string;
  media_type: string;
  byte_size: number;
  sha256: string;
  preview: string;
  note: string;
}

export class ApiRequestError extends Error {
  constructor(message: string, public status: number, public details: Record<string, unknown>) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000), ...init,
    });
  } catch {
    window.dispatchEvent(new Event("workbench:connection-lost"));
    throw new Error("无法连接本机服务。请启动 Rexak Agent；连接恢复后页面会自动重连，当前输入已保留。");
  }
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    let details: Record<string, unknown> = {};
    try {
      const body = (await response.json()) as { message?: string; detail?: string | Array<{msg?: string}>; details?: Record<string, unknown> };
      details = body.details ?? {};
      if (body.message) message = body.message;
      else if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg).filter(Boolean).join("；");
    } catch {
      // 非 JSON 错误仍保留 HTTP 状态，避免把返回页当作成功。
    }
    throw new ApiRequestError(message, response.status, details);
  }
  return (await response.json()) as T;
}

export class ApiClient {
  private csrf = "";

  async health(): Promise<void> {
    const response = await fetch("/health", { cache: "no-store", signal: AbortSignal.timeout(4000) });
    const body = await response.json() as { service?: string; database?: { ok: boolean } };
    if (!response.ok || body.service !== "agent-workbench-api" || !body.database?.ok) throw new Error("本机服务暂不可用");
  }

  async signIn(): Promise<void> {
    const ticket = await request<{ csrf_token: string }>("/session", { method: "POST" });
    this.csrf = ticket.csrf_token;
  }

  private write<T>(path: string, body?: unknown, idempotencyKey?: string): Promise<T> {
    if (!this.csrf) throw new Error("本地会话尚未建立");
    return request<T>(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Workbench-CSRF": this.csrf,
        ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  projects(): Promise<{ projects: ProjectItem[] }> {
    return request("/projects");
  }

  createProject(body: ProjectCreate): Promise<ProjectItem> {
    return this.write("/projects", body);
  }

  sessions(projectId: string, offset = 0): Promise<{ sessions: SessionItem[]; next_offset: number | null }> {
    return request(`/projects/${projectId}/sessions?limit=100&offset=${offset}`);
  }

  createSession(projectId: string, body: SessionCreate): Promise<{ session: SessionItem; run: RunItem }> {
    return this.write(`/projects/${projectId}/sessions`, body);
  }

  deleteSession(sessionId: string): Promise<{ deleted: boolean; cancellation_pending: boolean }> {
    return request(`/sessions/${sessionId}`, {
      method: "DELETE", headers: { "X-Workbench-CSRF": this.csrf },
    });
  }

  snapshot(sessionId: string): Promise<Snapshot> {
    return request(`/sessions/${sessionId}/snapshot`);
  }

  toolActivities(sessionId: string, offset = 0): Promise<components["schemas"]["ToolActivityPage"]> {
    return request(`/sessions/${sessionId}/tool-activity?limit=50&offset=${offset}`);
  }

  toolActivityDetail(sessionId: string, item: ToolActivityItem): Promise<ToolActivityDetail> {
    return request(`/sessions/${sessionId}/tool-activity/${item.run_id}/${encodeURIComponent(item.tool_call_id)}`);
  }

  plan(sessionId: string): Promise<PlanRead> {
    return request(`/sessions/${sessionId}/plan`);
  }

  eventPage(sessionId: string, before?: number): Promise<EventPage> {
    const query = before ? `?before=${before}&limit=100` : "?limit=100";
    return request(`/sessions/${sessionId}/events/page${query}`);
  }

  conversation(sessionId: string, cursor?: string): Promise<components["schemas"]["ConversationPage"]> {
    return request(`/sessions/${sessionId}/conversation?limit=30${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
  }

  conversationBody(sessionId: string, entry: ConversationEntry, offset = 0): Promise<ConversationBody> {
    return request(`/sessions/${sessionId}/conversation/${entry.source}/${entry.id}/text?offset=${offset}&limit=65536`);
  }

  async eventAt(sessionId: string, sequence: number): Promise<AgentEvent> {
    const page = await request<EventPage>(`/sessions/${sessionId}/events/page?before=${sequence + 1}&limit=1`);
    const event = page.events[0];
    if (!event || event.session_seq !== sequence) throw new Error("事件已不可读取，请刷新活动记录");
    return event;
  }

  findings(sessionId: string): Promise<{ findings: FindingItem[] }> {
    return request(`/sessions/${sessionId}/findings?limit=100`);
  }

  leads(sessionId: string): Promise<{ leads: LeadItem[] }> {
    return request(`/sessions/${sessionId}/leads?limit=100`);
  }

  jobs(sessionId: string): Promise<{ jobs: JobItem[] }> {
    return request(`/sessions/${sessionId}/jobs`);
  }

  usage(sessionId: string): Promise<UsageSummary> {
    return request(`/sessions/${sessionId}/usage`);
  }

  modelCalls(sessionId: string): Promise<components["schemas"]["ModelCallPage"]> {
    return request(`/sessions/${sessionId}/model-calls?limit=100`);
  }

  operations(sessionId: string): Promise<components["schemas"]["OperationPage"]> {
    return request(`/sessions/${sessionId}/operations?limit=100`);
  }

  spans(sessionId: string): Promise<components["schemas"]["SpanPage"]> {
    return request(`/sessions/${sessionId}/spans?limit=100`);
  }

  modelSettings(): Promise<ModelSettings> {
    return request("/settings/models");
  }

  priceCatalog(): Promise<PriceCatalog> {
    return request("/settings/prices");
  }

  updatePrice(body: PriceUpdate): Promise<PriceCatalog> {
    return this.write("/settings/prices", body);
  }

  artifact(artifactId: string): Promise<ArtifactPreview> {
    return request(`/artifacts/${artifactId}?limit=65536`);
  }

  reports(sessionId: string): Promise<{ reports: ReportItem[] }> {
    return request(`/sessions/${sessionId}/reports`);
  }

  message(sessionId: string, content: string): Promise<{ run_id: string | null }> {
    return this.write(`/sessions/${sessionId}/messages`, { content }, crypto.randomUUID());
  }

  control(runId: string, action: "pause" | "resume" | "cancel", acknowledgeUnknown = false): Promise<unknown> {
    return this.write(`/runs/${runId}/${action}`, acknowledgeUnknown ? { acknowledge_unknown_operations: true } : undefined, crypto.randomUUID());
  }

  createReport(sessionId: string): Promise<ReportItem> {
    return this.write(`/sessions/${sessionId}/reports`);
  }
}

export const api = new ApiClient();
