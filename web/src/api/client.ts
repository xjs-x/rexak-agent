import type { components } from "./generated/schema";

export type ProjectCreate = components["schemas"]["ProjectCreate"];
export type SessionCreate = components["schemas"]["SessionCreate"];
export type AgentEvent = components["schemas"]["EventEnvelope"];
export type SkillItem = components["schemas"]["SkillRead"];
export type SkillPage = components["schemas"]["SkillList"];
export type SkillResource = components["schemas"]["SkillResource"];

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

export type RunItem = components["schemas"]["RunSummaryRead"];

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
  /** `vulnerability / enabling_condition / unclassified`：弱点角色，不是严重等级。 */
  weakness_kind?: string;
  description?: string;
  impact?: string | null;
  evidence_refs?: string[];
  verification_id: string | null;
}

export interface WeaknessCondition {
  key: string;
  statement: string;
  limitations?: string | null;
  evidence_refs?: string[];
}

export interface WeaknessItem {
  id: string;
  title: string;
  category: string;
  status: string;
  weakness_kind: string;
  impact?: string | null;
  condition_keys?: string[];
  /** 是否已做过"后续用途判断"。未判断的弱点仍会被结束复查拦住。 */
  connection_reviewed?: boolean;
  /** 判断依据已过期，需要重新评估。 */
  connection_review_stale?: boolean;
  evidence_refs?: string[];
  verification_id: string | null;
  revision?: number;
}

export interface RelationItem {
  link_id: string;
  lead_id: string;
  weakness_id: string;
  role: string;
  relation_state: string;
  condition_key?: string | null;
  requirement_key?: string | null;
  source_weakness_revision?: number | null;
  revision?: number;
}

export interface WeaknessDetail {
  weakness: WeaknessItem & {
    description?: string;
    applicability?: Record<string, unknown>;
    provided_conditions?: WeaknessCondition[];
    connection_review?: { linked_lead_ids?: string[]; no_connection_note?: string | null } | Record<string, unknown>;
    connection_review_stale?: boolean;
  };
  relations: RelationItem[];
  related_leads: Array<{
    id: string;
    hypothesis: string;
    status: string;
    next_action?: string | null;
    required_conditions?: Array<Record<string, unknown>>;
    actionability?: string | null;
    revision?: number;
  }>;
  verifications: Array<{
    id: string;
    conclusion: string;
    method_version: string;
    performed_by: string;
    limitations?: string | null;
    created_at: string;
  }>;
  note: string;
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
  /** 当前能否推进；与 status（工作状态）是两个维度。 */
  required_conditions?: Array<Record<string, unknown>>;
  actionability?: string | null;
  actionability_note?: string | null;
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

export type ModelSettings = components["schemas"]["ModelSettingsRead"];

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
  skills(query = "", offset = 0): Promise<SkillPage> {
    return request(`/skills?query=${encodeURIComponent(query)}&offset=${offset}&limit=50`);
  }
  rescanSkills(): Promise<SkillPage> { return this.write("/skills/rescan"); }
  importSkill(body: {directory?: string; zip_base64?: string}): Promise<SkillItem> {
    return this.write("/skills/import", body);
  }
  skillVersions(id: string): Promise<SkillPage> { return request(`/skills/${id}/versions`); }
  skillResource(id: string, version: string, path = "SKILL.md", offset = 0, filesOffset = 0): Promise<SkillResource> {
    return request(`/skills/${id}/versions/${version}/files?path=${encodeURIComponent(path)}&offset=${offset}&files_offset=${filesOffset}`);
  }
  updateSkill(id: string, body: components["schemas"]["SkillUpdate"]): Promise<SkillPage> {
    return this.withSessionRetry(() => request(`/skills/${id}`, { method: "PATCH",
      headers: {"Content-Type": "application/json", "X-Workbench-CSRF": this.csrf}, body: JSON.stringify(body) }));
  }
  activeSkills(session: string): Promise<components["schemas"]["SkillSessionRead"]> {
    return request(`/sessions/${session}/skills`);
  }
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

  /**
   * 写请求统一入口：会话缺失时先建立，遇 401/403（会话与 Cookie 错位、服务重启、
   * 会话过期）时重新获取会话并重试一次。令牌在重试时重新读取，绝不缓存旧值。
   */
  private async withSessionRetry<T>(send: () => Promise<T>): Promise<T> {
    try {
      if (!this.csrf) await this.signIn();
      return await send();
    } catch (cause) {
      if (!(cause instanceof ApiRequestError) || (cause.status !== 401 && cause.status !== 403)) throw cause;
      await this.signIn();
      return await send();
    }
  }

  private write<T>(path: string, body?: unknown, idempotencyKey?: string): Promise<T> {
    return this.withSessionRetry(() =>
      request<T>(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Workbench-CSRF": this.csrf,
          ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
    );
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

  deleteSession(
    sessionId: string,
    terminateResources = false,
  ): Promise<{
    deleted: boolean;
    cancellation_pending: boolean;
    active_resources?: string[];
    resources_note?: string;
  }> {
    const suffix = terminateResources ? "?terminate_resources=true" : "";
    return this.withSessionRetry(() =>
      request(`/sessions/${sessionId}${suffix}`, {
        method: "DELETE", headers: { "X-Workbench-CSRF": this.csrf },
      }),
    );
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

  /** 弱点列表（与 findings 同一实体，附带弱点角色与用途判断状态）。 */
  weaknesses(sessionId: string): Promise<{ weaknesses: WeaknessItem[] }> {
    return request(`/sessions/${sessionId}/weaknesses?limit=200`);
  }

  /** 按需展开弱点详情与一跳关系（不为首屏加载全图）。 */
  weakness(sessionId: string, weaknessId: string): Promise<WeaknessDetail> {
    return request(`/sessions/${sessionId}/weaknesses/${weaknessId}`);
  }

  relations(sessionId: string, params: { weaknessId?: string; leadId?: string } = {}): Promise<{ relations: RelationItem[]; next_cursor: string | null }> {
    const query = new URLSearchParams({ limit: "100" });
    if (params.weaknessId) query.set("weakness_id", params.weaknessId);
    if (params.leadId) query.set("lead_id", params.leadId);
    return request(`/sessions/${sessionId}/exploration-relations?${query.toString()}`);
  }

  leads(sessionId: string): Promise<{ leads: LeadItem[] }> {
    return request(`/sessions/${sessionId}/leads?limit=100`);
  }

  jobs(sessionId: string): Promise<{ jobs: JobItem[] }> {
    return request(`/sessions/${sessionId}/jobs`);
  }

  // ---- 执行底座：资源 / 值班 / 输出（资源状态与值班状态分开） ----

  executionResources(
    sessionId: string,
    options: { refresh?: boolean; activeOnly?: boolean } = {},
  ): Promise<components["schemas"]["ExecutionResourcesResponse"]> {
    const query = new URLSearchParams({
      refresh: String(options.refresh ?? true),
      active_only: String(options.activeOnly ?? false),
      limit: "100",
    });
    return request(`/sessions/${sessionId}/execution/resources?${query.toString()}`);
  }

  executionResourceOutput(
    sessionId: string,
    resourceId: string,
    params: { view?: "stream" | "screen"; cursor?: string | null; stream?: string } = {},
  ): Promise<components["schemas"]["ResourceOutputResponse"]> {
    const query = new URLSearchParams({
      view: params.view ?? "stream",
      stream: params.stream ?? "stdout",
      limit: "16384",
    });
    if (params.cursor) query.set("cursor", params.cursor);
    return request(
      `/sessions/${sessionId}/execution/resources/${resourceId}/output?${query.toString()}`,
    );
  }

  closeExecutionResource(
    sessionId: string,
    resourceId: string,
  ): Promise<components["schemas"]["ExecutionOperationResponse"]> {
    return this.write(
      `/sessions/${sessionId}/execution/resources/${resourceId}/close`,
      undefined,
      crypto.randomUUID(),
    );
  }

  terminateAllExecutionResources(
    sessionId: string,
  ): Promise<components["schemas"]["TerminateAllResponse"]> {
    return this.write(
      `/sessions/${sessionId}/execution/resources/terminate-all`,
      undefined,
      crypto.randomUUID(),
    );
  }

  createDuty(
    sessionId: string,
    body: components["schemas"]["DutyCreateRequest"],
  ): Promise<components["schemas"]["DutyMutationResponse"]> {
    return this.write(`/sessions/${sessionId}/execution/duties`, body, crypto.randomUUID());
  }

  cancelDuty(
    sessionId: string,
    delegationId: string,
    reason: string,
  ): Promise<components["schemas"]["DutyMutationResponse"]> {
    return this.write(
      `/sessions/${sessionId}/execution/duties/${delegationId}/cancel`,
      { reason },
      crypto.randomUUID(),
    );
  }

  executionEnvironment(sessionId: string): Promise<components["schemas"]["ExecutionEnvironmentReport"]> {
    return request(`/sessions/${sessionId}/execution/environment`);
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

  saveModelSettings(body: components["schemas"]["ModelSettingsUpdate"]): Promise<ModelSettings> {
    return this.withSessionRetry(() =>
      request("/settings/models", { method: "PUT", headers: { "Content-Type": "application/json", "X-Workbench-CSRF": this.csrf }, body: JSON.stringify(body) }),
    );
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

  message(sessionId: string, content: string, model?: { provider_id: string; model_name: string }, skillRefs?: string[]): Promise<{ run_id: string | null }> {
    return this.write(`/sessions/${sessionId}/messages`, { content, ...model, ...(skillRefs?.length ? {skill_refs: skillRefs} : {}) }, crypto.randomUUID());
  }

  control(runId: string, action: "pause" | "resume" | "cancel", acknowledgeUnknown = false): Promise<unknown> {
    return this.write(`/runs/${runId}/${action}`, acknowledgeUnknown ? { acknowledge_unknown_operations: true } : undefined, crypto.randomUUID());
  }

  createReport(sessionId: string): Promise<ReportItem> {
    return this.write(`/sessions/${sessionId}/reports`);
  }
}

export const api = new ApiClient();
