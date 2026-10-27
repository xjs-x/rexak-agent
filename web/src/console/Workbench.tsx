import type {
  AgentEvent,
  LeadItem,
  ModelSettings,
  PlanRead,
  RunItem,
  Snapshot,
  ToolActivityItem,
  UsageSummary,
  WeaknessItem,
} from "../api/client";
import { Icon } from "./Icon";
import {
  PLAN_STATUS_LABELS,
  canCancel,
  canPause,
  canResume,
  classifyTool,
  durationLabel,
  formatShortTime,
  modeLabel,
  runLabel,
  runTone,
  shortId,
  toolStateLabel,
  toolTitle,
} from "./labels";

function statusTone(status: string): string {
  if (status === "in_progress") return "run";
  if (status === "done") return "";
  if (status === "blocked") return "block";
  return "";
}

export function Workbench({
  projectName,
  snapshot,
  run,
  plan,
  tools,
  weaknesses,
  leads,
  usage,
  models,
  inputRequired,
  onControl,
  onOpenDrawer,
  onOpenWeakness,
}: {
  projectName: string;
  snapshot: Snapshot | null;
  run: RunItem | null;
  plan: PlanRead | null;
  tools: ToolActivityItem[];
  weaknesses: WeaknessItem[];
  leads: LeadItem[];
  usage: UsageSummary | null;
  models: ModelSettings | null;
  inputRequired: AgentEvent | null;
  onControl: (action: "pause" | "resume" | "cancel") => void;
  onOpenDrawer: (tab: string) => void;
  onOpenWeakness: (id: string) => void;
}) {
  if (!snapshot || !run) {
    return (
      <section className="view work-view">
        <div className="work-empty">
          <div className="box"><Icon name="activity" size="lg" /></div>
          <h3>还没有可展示的运行</h3>
          <p>从左侧选择一个任务；运行器接手后，计划、工具操作和用量会从真实接口读取。</p>
        </div>
      </section>
    );
  }

  const scope = snapshot.session.scope;
  const modelName = `${run.provider_id}/${run.model_name}`;
  const profile = models?.profiles.find(
    (item) => item.provider_id === run.provider_id && item.model_name === run.model_name,
  );

  const steps = plan?.steps ?? [];
  const done = steps.filter((step) => step.status === "done").length;
  const progress = steps.length ? Math.round((done / steps.length) * 100) : 0;

  const confirmed = weaknesses.filter((item) => item.status === "confirmed").length;
  const candidate = weaknesses.filter((item) => item.status === "candidate").length;
  const inconclusive = weaknesses.filter((item) => item.status === "inconclusive").length;
  const disproved = weaknesses.filter((item) => item.status === "disproved").length;
  const weaknessTotal = weaknesses.length || 1;

  const actionability = (lead: LeadItem) => lead.actionability ?? "needs_review";
  const readyLeads = leads.filter((lead) => actionability(lead) === "ready").length;
  const needCondition = leads.filter((lead) => actionability(lead) === "needs_condition").length;
  const blockedLeads = leads.filter((lead) => ["blocked", "deferred"].includes(lead.status)).length;

  const payload = (inputRequired?.payload ?? {}) as Record<string, unknown>;
  const currencies = usage ? Object.entries(usage.by_currency) : [];

  const latestTools = tools
    .filter((item) => item.run_id === run.id)
    .sort((left, right) => Date.parse(right.started_at) - Date.parse(left.started_at))
    .slice(0, 8);

  return (
    <section className="view work-view" aria-label="运行台">
      <div className="work-grid">
        <div className="col">
          <section className="card">
            <div className="card-h">
              <h3>本次运行</h3>
              <span className="more">RUN-{shortId(run.id, 6)}</span>
            </div>
            <div className={`big-status ${runTone(run.status)}`}>
              <span className="led" />
              <span>{runLabel(run.status)}</span>
            </div>
            <p className="big-sub">
              开始于 {formatShortTime(run.created_at)}
              {run.control_version > 0 ? ` · 控制版本 ${run.control_version}` : ""}
            </p>
            <p className="objective">{run.objective || snapshot.session.title}</p>
            <div className="fact-grid">
              <div className="fact"><small>项目</small><strong>{projectName}</strong></div>
              <div className="fact"><small>范围模式</small><strong>{modeLabel(scope?.mode)}</strong></div>
              <div className="fact"><small>计划版本</small><strong>{plan ? `v${plan.revision} · ${steps.length} 步` : "未读取"}</strong></div>
              <div className="fact"><small>模型</small><strong title={modelName}>{run.model_name}</strong></div>
            </div>
            <div className="controls">
              <button className="btn" disabled={!canPause(run.status)} onClick={() => onControl("pause")}>暂停</button>
              <button className="btn" disabled={!canResume(run.status)} onClick={() => onControl("resume")}>继续</button>
              <button className="btn danger" disabled={!canCancel(run.status)} onClick={() => onControl("cancel")}>取消</button>
            </div>
            {!profile?.secret_configured && (
              <p className="fine">当前模型未配置 Key；新的模型请求会失败。</p>
            )}
            <div className="constraints">
              <span><Icon name="db" size="sm" />状态以 checkpoint 与执行台账为准</span>
              <span><Icon name="clock" size="sm" />暂停、取消要等待执行端确认</span>
              <span><Icon name="gauge" size="sm" />未确认的操作不会自动重做</span>
            </div>
          </section>

          <section className="card">
            <div className="card-h"><h3>本轮范围</h3><span className="more">{scope ? `v${scope.revision}` : "未读取"}</span></div>
            {scope ? (
              <>
                <div className="d-row"><span className="k">模式</span><span className="v">{modeLabel(scope.mode)}</span></div>
                {(scope.rules ?? []).length ? (
                  (scope.rules ?? []).map((rule, index) => (
                    <div className="scope-rule" key={`${rule.host}-${index}`}>
                      <span>{rule.scheme || "any"}://{rule.host}{rule.port ? `:${rule.port}` : ""}</span>
                      {rule.include_subdomains && <span>· 含子域</span>}
                    </div>
                  ))
                ) : (
                  <p className="fine" style={{ marginTop: 0 }}>
                    按指令运行：目标在用户消息里声明，由后端解析并写入范围快照。
                  </p>
                )}
                <p className="fine">范围变更只能通过后端范围接口应用；当前界面只读。</p>
              </>
            ) : (
              <p className="fine">尚未读取到范围快照。</p>
            )}
          </section>
        </div>

        <div className="col">
          <section className="card">
            <div className="card-h">
              <h3>当前计划</h3>
              <span className="more">{plan ? `v${plan.revision} · ${steps.length} 步` : "未读取"}</span>
            </div>
            {steps.length ? (
              <>
                <div className="plan-list">
                  {steps.map((step, index) => (
                    <div
                      className={`step ${step.status === "done" ? "done" : step.status === "in_progress" ? "active" : ""}`}
                      key={step.step_id}
                    >
                      <span className="marker">{step.status === "done" ? <Icon name="check" size="sm" /> : index + 1}</span>
                      <div>
                        <div className="s-title">{step.step_id} · {step.description}</div>
                        <div className="s-meta">
                          <span>{step.rationale || "未记录说明"}</span>
                          {step.lead_id && <span>线索 {shortId(step.lead_id, 10)}</span>}
                        </div>
                      </div>
                      <span className={`s-pill ${statusTone(step.status)}`}>
                        {PLAN_STATUS_LABELS[step.status] ?? step.status}
                      </span>
                    </div>
                  ))}
                </div>
                <div className="plan-foot"><span>{done} / {steps.length}</span><span className="bar"><i style={{ width: `${progress}%` }} /></span><span>{progress}%</span></div>
              </>
            ) : (
              <p className="fine">{plan ? "尚无计划步骤；模型可能直接执行工具。" : "正在读取计划…"}</p>
            )}
            {plan?.current_focus && (
              <div className="d-row" style={{ borderBottom: 0, marginTop: 4 }}>
                <span className="k">当前焦点</span>
                <span className="v">
                  {plan.current_focus.step_id || "未记录步骤"}
                  {" · "}
                  {plan.current_focus.intent || plan.current_focus.reason || "焦点未记录说明"}
                </span>
              </div>
            )}
          </section>

          <section className="card">
            <div className="card-h"><h3>最近工具操作</h3><span className="more">共 {tools.length} 项</span></div>
            {latestTools.length ? (
              latestTools.map((item) => (
                <button
                  className="op"
                  key={`${item.run_id}:${item.tool_call_id}`}
                  style={{ width: "100%", textAlign: "left" }}
                  onClick={() => onOpenDrawer("activity")}
                >
                  <span className="oi">
                    <Icon name={classifyTool(item.tool_name) === "command" ? "terminal" : classifyTool(item.tool_name) === "edit" ? "pencil" : "layers"} size="sm" />
                  </span>
                  <span>{toolTitle(item.tool_name)}</span>
                  <span className="on">{item.tool_name !== toolTitle(item.tool_name) ? item.tool_name : ""}</span>
                  <span className="ot">
                    {toolStateLabel(item.status)}
                    {item.finished_at ? ` · ${durationLabel(item.started_at, item.finished_at)}` : ""}
                  </span>
                </button>
              ))
            ) : (
              <p className="fine">本轮还没有工具操作。</p>
            )}
            <p className="fine" style={{ marginTop: 10 }}>这里只显示摘要；参数与输出在任务详情 → 实时活动逐条查看。</p>
          </section>
        </div>

        <div className="col col-right">
          <section className="card tight">
            <div className="card-h"><h3>需要你决定</h3><span className="more">{inputRequired ? "1 项" : "无"}</span></div>
            {inputRequired ? (
              <div className="decision">
                <strong>{String(payload.reason ?? "等待你的输入")}</strong>
                <p>{String(payload.message ?? run.stop_reason ?? "运行停在等待输入的检查点。")}</p>
                <div className="mini-actions">
                  {canResume(run.status) && (
                    <button className="mini-btn strong" onClick={() => onControl("resume")}>继续运行</button>
                  )}
                  <button className="mini-btn" onClick={() => onOpenDrawer("activity")}>查看活动</button>
                </div>
              </div>
            ) : (
              <p className="fine" style={{ marginTop: 0 }}>当前没有待确认请求。需要确认时，这里会出现真实输入请求。</p>
            )}
          </section>

          <section className="card tight">
            <div className="card-h"><h3>弱点</h3><span className="more">共 {weaknesses.length} 条</span></div>
            <div className="stat"><span>已确认（有验证记录）</span><strong>{confirmed}<small>条</small></strong></div>
            <div className="stat"><span>候选待验证</span><strong>{candidate}<small>条</small></strong></div>
            <div className="stat"><span>证据不足 / 未支持</span><strong>{inconclusive + disproved}<small>条</small></strong></div>
            <div className="stack">
              <i style={{ width: `${(confirmed / weaknessTotal) * 100}%`, background: "#2f9e73" }} />
              <i style={{ width: `${(candidate / weaknessTotal) * 100}%`, background: "#d9c39a" }} />
              <i style={{ width: `${((inconclusive + disproved) / weaknessTotal) * 100}%`, background: "#dcdce1" }} />
            </div>
            <div className="legend-mini">
              <span><b style={{ background: "#2f9e73" }} />已确认</span>
              <span><b style={{ background: "#d9c39a" }} />候选</span>
              <span><b style={{ background: "#dcdce1" }} />其他</span>
            </div>
            {weaknesses.slice(0, 2).map((item) => (
              <button className="d-row" key={item.id} style={{ width: "100%", textAlign: "left" }} onClick={() => onOpenWeakness(item.id)}>
                <span className="v">{item.title}</span>
                <span className="r">{item.status === "confirmed" ? "已确认" : "候选"}</span>
              </button>
            ))}
            <p className="fine">候选不计入已确认；组合弱点必须另有验证记录。</p>
          </section>

          <section className="card tight">
            <div className="card-h"><h3>线索</h3><span className="more">共 {leads.length} 条</span></div>
            <div className="stat"><span>可推进</span><strong>{readyLeads}</strong></div>
            <div className="stat"><span>缺条件</span><strong>{needCondition}</strong></div>
            <div className="stat"><span>受阻 / 暂缓</span><strong>{blockedLeads}</strong></div>
            <p className="fine">
              {leads.length
                ? "受阻与暂缓保留原因，不因为“暂时做不了”而被隐藏。"
                : "还没有线索记录。"}
            </p>
          </section>

          <section className="card tight">
            <div className="card-h"><h3>覆盖</h3><span className="more">口径：已登记检查</span></div>
            <div className="coverage-num">{snapshot.coverage.total}<small>项</small></div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, margin: "7px 0 4px" }}>
              {Object.entries(snapshot.coverage.by_status).map(([key, value]) => (
                <span className="s-pill" key={key}>{key} · {value}</span>
              ))}
            </div>
            <p className="fine">{snapshot.coverage.ratio_definition || "尚未登记覆盖；不能据此推断目标没有漏洞。"}</p>
          </section>

          <section className="card tight">
            <div className="card-h"><h3>用量</h3><span className="more">本轮</span></div>
            {usage ? (
              <>
                <div className="stat"><span>模型请求</span><strong>{usage.call_count}</strong></div>
                {currencies.length ? currencies.map(([currencyCode, value]) => (
                  <div className="stat" key={currencyCode}>
                    <span>已知费用（{currencyCode}）</span><strong>{value.known_cost}</strong>
                  </div>
                )) : <div className="stat"><span>已知费用</span><strong>暂无数据</strong></div>}
                <div className="stat"><span>未定价请求</span><strong>{usage.unpriced_calls}</strong></div>
                <div className="stat"><span>用量未知请求</span><strong>{usage.unknown_usage_calls}</strong></div>
                <p className="fine">{usage.note}</p>
              </>
            ) : (
              <p className="fine">正在读取用量…</p>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}
