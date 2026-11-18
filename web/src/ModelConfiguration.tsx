import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiRequestError, type ModelSettings } from "./api/client";
import type { components } from "./api/generated/schema";
import { CatalogDialog, CatalogModelEditor, DiscoveryPicker, type CatalogModel } from "./ModelCatalog";
import "./model-configuration.css";

type Provider = components["schemas"]["ProviderRead"];
type ProviderWrite = components["schemas"]["ProviderWrite"];
type Directory = components["schemas"]["ModelDiscoveryRead"];
type ModelSettingsProps = {
  settings: ModelSettings | null;
  onSaved: (settings: ModelSettings) => void;
};

function errorField(error: unknown): string | undefined {
  if (!(error instanceof ApiRequestError)) return undefined;
  if (typeof error.details.field === "string") return error.details.field;
  const fields = error.details.errors as { loc?: string[] }[] | undefined;
  return fields?.[0]?.loc?.[1];
}

export function ModelConfiguration({ settings, onSaved }: ModelSettingsProps) {
  const [editor, setEditor] = useState<{ provider: Provider | null; revision: number; defaultModel?: string }>();
  const [deletion, setDeletion] = useState<{ provider: Provider; revision: number }>();
  const [deleting, setDeleting] = useState(false);
  const [expanded, setExpanded] = useState<string>();
  const [expandedCount, setExpandedCount] = useState(60);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  if (!settings) return <p role="status">读取模型配置中…</p>;
  const edit = (provider: Provider | null) => {
    setMessage(""); setError("");
    setEditor({ provider, revision: settings.revision ?? 0,
      defaultModel: provider?.provider_id === settings.default_provider ? settings.default_model : undefined });
  };
  if (editor) return <ProviderEditor {...editor} onBack={() => setEditor(undefined)} onSaved={(updated) => {
    onSaved(updated); setEditor(undefined); setMessage("已保存模型设置。");
  }} />;
  const providers = settings.providers ?? [];
  return <section className="model-configuration">
    <header className="catalog-heading">
      <div><h3>提供商</h3><p>{settings.effective_scope}</p></div>
      <button type="button" className="primary" onClick={() => edit(null)}>新增提供商</button>
    </header>
    {!providers.length && <p className="catalog-empty">尚未配置提供商。新增后可以获取目录或手动添加模型。</p>}
    <ul className="catalog-rows">
      {providers.map((provider) => {
        const open = expanded === provider.provider_id;
        return <li key={provider.provider_id}>
          <div className="catalog-provider-row">
            <div className="catalog-identity">
              <strong>{provider.display_name}</strong><span className="catalog-id">{provider.provider_id}</span>
              <span className="catalog-endpoint" title={provider.base_url}>{provider.base_url || "未填写 API 地址"}</span>
              <small>OpenAI Chat Completions · {provider.secret_configured ? "Key 已配置" : "Key 未配置"} · {provider.enabled === false ? "已停用" : "已启用"}</small>
            </div>
            <div className="catalog-inline">
              <button type="button" aria-expanded={open} onClick={() => {
                setExpanded(open ? undefined : provider.provider_id); setExpandedCount(60);
              }}>
                {open ? "收起模型" : "查看模型"}（{provider.models?.length ?? 0}）
              </button>
              <button type="button" aria-label={`编辑 ${provider.display_name}`} onClick={() => edit(provider)}>编辑</button>
              <button type="button" aria-label={`删除 ${provider.display_name}`} onClick={() => {
                setError(""); setMessage(""); setDeletion({ provider, revision: settings.revision ?? 0 });
              }}>删除</button>
            </div>
          </div>
          {open && <ul className="catalog-expanded">
            {!(provider.models ?? []).length && <li>尚未添加模型。</li>}
            {(provider.models ?? []).slice(0, expandedCount).map((model) => <li key={model.model_id}>
              <span>{model.display_name || model.model_id}</span>
              {model.display_name && <small>{model.model_id}</small>}
              {settings.default_provider === provider.provider_id && settings.default_model === model.model_id && <small>默认</small>}
              {model.enabled === false && <small>已停用</small>}
            </li>)}
            {(provider.models?.length ?? 0) > expandedCount && <li>
              <button type="button" onClick={() => setExpandedCount((count) => count + 60)}>显示更多型号</button>
            </li>}
          </ul>}
        </li>;
      })}
    </ul>
    {message && <p className="catalog-feedback" role="status">{message}</p>}
    {deletion && <CatalogDialog title="删除提供商" onClose={() => !deleting && setDeletion(undefined)}>
      <p>从模型设置中移除「{deletion.provider.display_name}」及其模型。历史阶段会保留原有绑定和凭据引用。</p>
      {error && <p className="catalog-error" role="alert">{error}</p>}
      <div className="catalog-actions">
        <button type="button" disabled={deleting} onClick={() => setDeletion(undefined)}>取消</button>
        <button type="button" className="danger" disabled={deleting} onClick={async () => {
          setDeleting(true); setError("");
          try {
            onSaved(await api.deleteModelProvider(deletion.provider.provider_id, deletion.revision));
            setDeletion(undefined); setMessage("已删除提供商。");
          } catch (cause) { setError(cause instanceof Error ? cause.message : "删除失败。"); }
          finally { setDeleting(false); }
        }}>{deleting ? "正在删除…" : "确认删除提供商"}</button>
      </div>
    </CatalogDialog>}
  </section>;
}

function ProviderEditor({ provider, revision, defaultModel, onBack, onSaved }: {
  provider: Provider | null; revision: number; defaultModel?: string;
  onBack: () => void; onSaved: (updated: ModelSettings) => void;
}) {
  const [draft, setDraft] = useState<ProviderWrite>(() => ({
    revision, provider_id: provider?.provider_id ?? "", display_name: provider?.display_name ?? "",
    base_url: provider?.base_url ?? "", enabled: provider?.enabled ?? true,
    protocol: provider?.protocol ?? "openai-chat-completions",
    models_url: provider?.models_url ?? "", discovery_format: provider?.discovery_format ?? "openai",
    models: provider?.models ?? [], delete_api_key: false,
  }));
  const [key, setKey] = useState("");
  const [defaultChoice, setDefaultChoice] = useState(defaultModel);
  const [query, setQuery] = useState("");
  const [visibleCount, setVisibleCount] = useState(60);
  const [modelEditor, setModelEditor] = useState<CatalogModel | null>();
  const [directory, setDirectory] = useState<Directory>();
  const [pending, setPending] = useState<"save" | "discover">();
  const [problem, setProblem] = useState<{ message: string; field?: string }>();
  const [feedback, setFeedback] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const models = draft.models ?? [];
  const ids = useMemo(() => new Set(models.map((model) => model.model_id)), [models]);
  const term = query.trim().toLowerCase();
  const visible = models.filter((model) => [model.model_id, model.display_name].some((value) => value?.toLowerCase().includes(term)));
  const fieldError = (field: string) => problem?.field === field ? <small className="catalog-error" role="alert">{problem.message}</small> : undefined;
  const set = <K extends keyof ProviderWrite>(field: K, value: ProviderWrite[K]) => {
    setDraft((current) => ({ ...current, [field]: value }));
    if (problem?.field === field) setProblem(undefined);
  };
  const makeProblem = (cause: unknown, fallback: string) => {
    const message = cause instanceof Error ? cause.message : fallback;
    setProblem({ message: key.trim().length >= 8 && message.includes(key.trim()) ? fallback : message, field: errorField(cause) });
  };
  const discover = async () => {
    setProblem(undefined); setFeedback("");
    if (!draft.base_url.trim()) { setProblem({ field: "base_url", message: "请填写 API Base URL。" }); return; }
    if (!key.trim() && (!provider?.secret_configured || draft.delete_api_key)) {
      setProblem({ field: "api_key", message: "请填写 API Key 后获取可用模型。" }); return;
    }
    const abort = new AbortController();
    controller.current = abort;
    setPending("discover");
    try {
      const result = await api.discoverModels({
        base_url: draft.base_url, protocol: draft.protocol, models_url: draft.models_url?.trim() || null,
        discovery_format: draft.discovery_format, saved_provider_id: provider?.provider_id,
        api_key: key.trim() || undefined,
      }, abort.signal);
      if (!abort.signal.aborted) setDirectory(result);
    } catch (cause) {
      if (!abort.signal.aborted) makeProblem(cause, "无法获取模型目录，可以手动添加模型。");
    } finally {
      controller.current = null;
      setPending(undefined);
    }
  };
  return <section className="model-configuration">
    <header className="catalog-heading">
      <div><button className="catalog-back" type="button" disabled={pending === "save"} onClick={onBack}>‹ 返回提供商</button>
        <h3>{provider ? `编辑 ${provider.display_name}` : "新增提供商"}</h3></div>
    </header>
    <form noValidate onSubmit={async (event) => {
      event.preventDefault(); setProblem(undefined);
      if (pending) return;
      if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/.test(draft.provider_id.trim())) {
        setProblem({ field: "provider_id", message: "请输入 Provider ID，可使用字母、数字、点、连字符和下划线，首字符须为字母或数字。" }); return;
      }
      if (!draft.display_name.trim()) { setProblem({ field: "display_name", message: "请输入显示名称。" }); return; }
      if (!draft.base_url.trim()) { setProblem({ field: "base_url", message: "请填写 API Base URL。" }); return; }
      setPending("save");
      try {
        onSaved(await api.saveModelProvider({
          ...draft, provider_id: draft.provider_id.trim(), display_name: draft.display_name.trim(),
          models_url: draft.models_url?.trim() || null, api_key: key.trim() || undefined,
          default_model: draft.enabled !== false && models.some((model) => model.enabled !== false && model.model_id === defaultChoice)
            ? defaultChoice : undefined,
        }, !!provider));
      } catch (cause) { makeProblem(cause, "无法保存模型设置。"); }
      finally { setPending(undefined); }
    }}>
      <fieldset className="catalog-connection" disabled={!!pending}>
        <legend>连接设置</legend>
        <div className="catalog-field-grid">
          <label>Provider ID<input value={draft.provider_id} readOnly={!!provider} spellCheck={false} maxLength={64}
            placeholder="例如 office-gateway" aria-invalid={problem?.field === "provider_id"} onChange={(event) => set("provider_id", event.target.value)} />
            {fieldError("provider_id")}</label>
          <label>显示名称<input value={draft.display_name} maxLength={160} placeholder="例如 办公网关"
            aria-invalid={problem?.field === "display_name"} onChange={(event) => set("display_name", event.target.value)} />{fieldError("display_name")}</label>
          <label>API Base URL<input value={draft.base_url} maxLength={2048} spellCheck={false} placeholder="https://gateway.example/v1"
            aria-invalid={problem?.field === "base_url"} onChange={(event) => set("base_url", event.target.value)} />{fieldError("base_url")}</label>
          <label>API 协议<select value={draft.protocol} onChange={() => set("protocol", "openai-chat-completions")}>
            <option value="openai-chat-completions">OpenAI Chat Completions</option>
          </select></label>
        </div>
        <label>API Key<input type="password" autoComplete="new-password" value={key} maxLength={4096} disabled={draft.delete_api_key}
          placeholder={provider?.secret_configured ? "已配置，留空保留" : "填写此提供商的 Key"} aria-invalid={problem?.field === "api_key"}
          onChange={(event) => { setKey(event.target.value); if (problem?.field === "api_key") setProblem(undefined); }} />
          {fieldError("api_key")}</label>
        <div className="catalog-inline">
          <label className="catalog-check"><input type="checkbox" checked={draft.enabled !== false}
            onChange={(event) => set("enabled", event.target.checked)} />启用提供商</label>
          {provider?.secret_configured && <label className="catalog-check"><input type="checkbox" checked={draft.delete_api_key ?? false}
            onChange={(event) => { setKey(""); set("delete_api_key", event.target.checked); }} />保存时清除 Key</label>}
        </div>
        <details className="catalog-advanced" open={problem?.field === "models_url" ? true : undefined}>
          <summary>高级连接设置</summary>
          <label>模型列表 API URL<input value={draft.models_url ?? ""} maxLength={2048} spellCheck={false}
            placeholder="留空时使用 Base URL /models" aria-invalid={problem?.field === "models_url"}
            onChange={(event) => set("models_url", event.target.value)} />{fieldError("models_url")}</label>
          <label>目录格式<select value={draft.discovery_format}
            onChange={(event) => set("discovery_format", event.target.value as ProviderWrite["discovery_format"])}>
            <option value="openai">OpenAI 模型列表</option><option value="dashscope">阿里云百炼模型目录</option>
          </select></label>
          <p>百炼目录格式需要填写官方模型列表地址。跨主机查询时需为目标地址输入 Key。</p>
        </details>
      </fieldset>
      <section className="catalog-provider-models">
        <header className="catalog-heading">
          <div><h3>模型目录</h3><p>已添加 {models.length} 个模型</p></div>
          <div className="catalog-inline">
            {pending === "discover" ? <>
              <span role="status">正在获取模型…</span>
              <button type="button" onClick={() => { controller.current?.abort(); setFeedback("已取消目录查询。"); }}>取消查询</button>
            </> : <button type="button" disabled={!!pending} onClick={() => void discover()}>获取可用模型</button>}
            <button type="button" disabled={!!pending} onClick={() => setModelEditor(null)}>手动添加模型</button>
          </div>
        </header>
        {models.length > 0 && <label>搜索已添加模型<input type="search" value={query}
          onChange={(event) => { setQuery(event.target.value); setVisibleCount(60); }} /></label>}
        {!visible.length && <p className="catalog-empty">{models.length ? "没有匹配的型号。" : "尚未添加模型。可以查询目录或填写真实 Model ID。"}</p>}
        <ul className="catalog-rows">
          {visible.slice(0, visibleCount).map((model) => <li className="catalog-provider-row" key={model.model_id}>
            <div className="catalog-identity">
              <strong>{model.display_name || model.model_id}</strong>
              {model.display_name && <small>{model.model_id}</small>}
              <small>{model.enabled === false ? "已停用" : model.model_id === defaultChoice && draft.enabled !== false ? "默认模型" : "能力待验证"}</small>
            </div>
            <div className="catalog-inline">
              {model.model_id !== defaultChoice && model.enabled !== false && draft.enabled !== false &&
                <button type="button" disabled={!!pending} onClick={() => setDefaultChoice(model.model_id)}>设为默认</button>}
              <button type="button" disabled={!!pending} aria-label={`编辑模型 ${model.model_id}`} onClick={() => setModelEditor(model)}>编辑</button>
              <button type="button" disabled={!!pending} aria-label={`移除模型 ${model.model_id}`}
                onClick={() => {
                  set("models", models.filter((item) => item.model_id !== model.model_id));
                  if (defaultChoice === model.model_id) setDefaultChoice(undefined);
                }}>移除</button>
            </div>
          </li>)}
        </ul>
        {visibleCount < visible.length && <button type="button" onClick={() => setVisibleCount((count) => count + 60)}>显示更多型号</button>}
      </section>
      {problem && !["provider_id", "display_name", "base_url", "api_key", "models_url"].includes(problem.field ?? "") &&
        <p className="catalog-error" role="alert">{problem.message}</p>}
      {feedback && <p className="catalog-feedback" role="status">{feedback}</p>}
      <div className="catalog-actions">
        <button type="button" onClick={onBack} disabled={pending === "save"}>取消编辑</button>
        <button type="submit" className="primary" disabled={!!pending}>{pending === "save" ? "正在保存…" : "保存提供商"}</button>
      </div>
    </form>
    {directory && <DiscoveryPicker result={directory} existing={ids} onClose={() => setDirectory(undefined)} onAdd={(added) => {
      const unique = added.filter((model) => !ids.has(model.model_id));
      set("models", [...models, ...unique]); setDirectory(undefined); setFeedback(`已加入 ${unique.length} 个模型，保存后生效。`);
    }} />}
    {modelEditor !== undefined && <CatalogModelEditor model={modelEditor} providerName={draft.display_name} ids={ids}
      defaultModel={!!modelEditor && modelEditor.model_id === defaultChoice} maySetDefault={draft.enabled !== false}
      onClose={() => setModelEditor(undefined)} onSave={(model, makeDefault) => {
        if (makeDefault) setDefaultChoice(model.model_id);
        else if (modelEditor?.model_id === defaultChoice && defaultChoice) setDefaultChoice(undefined);
        set("models", modelEditor ? models.map((item) => item.model_id === modelEditor.model_id ? model : item) : [...models, model]);
        setModelEditor(undefined);
      }} />}
  </section>;
}
