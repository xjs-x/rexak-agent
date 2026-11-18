import { useDeferredValue, useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { components } from "./api/generated/schema";

export type CatalogModel = components["schemas"]["CatalogModel"];
type Capability = NonNullable<CatalogModel["supports_tools"]>;

export function CatalogDialog({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className="catalog-dialog modal" aria-labelledby={id}
    onKeyDown={(event) => { if (event.key === "Escape") event.stopPropagation(); }}
    onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <h2 id={id}>{title}</h2>
    {children}
  </dialog>;
}

export function DiscoveryPicker({ result, existing, onClose, onAdd }: {
  result: components["schemas"]["ModelDiscoveryRead"];
  existing: Set<string>;
  onClose: () => void;
  onAdd: (models: CatalogModel[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(new Set<string>());
  const [visibleCount, setVisibleCount] = useState(80);
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const filtered = result.models.filter((model) => model.model_id.toLowerCase().includes(deferredQuery));
  const available = filtered.filter((model) => !existing.has(model.model_id));
  const allSelected = available.length > 0 && available.every((model) => selected.has(model.model_id));
  const list = useRef<HTMLDivElement>(null);
  return <CatalogDialog title="选择要添加的模型" onClose={onClose}>
    <p>以下是模型提供商返回的模型，勾选需要添加的型号。调用能力待验证。</p>
    {result.truncated && <p role="status">目录已达查询上限。其他型号可以手动添加。</p>}
    <label>搜索可用模型<input autoFocus type="search" value={query} onChange={(event) => {
      setQuery(event.target.value); setVisibleCount(80); if (list.current) list.current.scrollTop = 0;
    }} /></label>
    <div className="catalog-picker-tools">
      <button type="button" disabled={!available.length || allSelected} onClick={() => {
        setSelected((current) => new Set([...current, ...available.map((item) => item.model_id)]));
      }}>全选筛选结果</button>
      <button type="button" disabled={!selected.size} onClick={() => setSelected(new Set())}>取消全选</button>
      <span role="status">已选 {selected.size} 项</span>
    </div>
    <div className="catalog-picker-list" ref={list}>
      {!filtered.length && <p>{result.models.length ? "没有匹配的型号。" : "模型服务返回了空目录。"}</p>}
      {filtered.slice(0, visibleCount).map((model) => <label key={model.model_id} className="catalog-check">
        <input type="checkbox" checked={selected.has(model.model_id)} disabled={existing.has(model.model_id)}
          onChange={({ target }) => {
            const checked = target.checked;
            setSelected((current) => {
              const next = new Set(current);
              if (checked) next.add(model.model_id); else next.delete(model.model_id);
              return next;
            });
          }} />
        <span>{model.model_id}</span>
        {existing.has(model.model_id) && <small>已添加</small>}
      </label>)}
      {visibleCount < filtered.length && <button type="button" onClick={() => setVisibleCount((count) => count + 80)}>显示更多型号</button>}
    </div>
    <div className="catalog-actions">
      <button type="button" onClick={onClose}>取消</button>
      <button type="button" className="primary" disabled={!selected.size}
        onClick={() => onAdd(result.models.filter((model) => selected.has(model.model_id)))}>添加所选</button>
    </div>
  </CatalogDialog>;
}

const numericalFields = [
  ["context_window", "上下文窗口"], ["max_input_tokens", "最大输入长度"],
  ["max_output_tokens", "最大输出长度"], ["request_output_limit", "单次请求输出预算"],
] as const;
const capabilityFields = [
  ["supports_reasoning", "Thinking / Reasoning"], ["supports_tools", "Function Calling"],
  ["supports_images", "图像输入"],
] as const;

export function CatalogModelEditor({ model, providerName, ids, defaultModel, maySetDefault, onClose, onSave }: {
  model: CatalogModel | null;
  providerName: string;
  ids: Set<string>;
  defaultModel: boolean;
  maySetDefault: boolean;
  onClose: () => void;
  onSave: (updated: CatalogModel, makeDefault: boolean) => void;
}) {
  const [draft, setDraft] = useState<CatalogModel>(model ?? {
    model_id: "", display_name: "", enabled: true, supports_reasoning: "unknown",
    supports_tools: "unknown", supports_images: "unknown", specification_source: "unknown",
  });
  const [makeDefault, setDefault] = useState(defaultModel);
  const [error, setError] = useState("");
  return <CatalogDialog title={model ? "编辑模型" : "手动添加模型"} onClose={onClose}>
    <p>所属提供商 {providerName || "当前提供商"}</p>
    <form className="catalog-model-form" onSubmit={(event) => {
      event.preventDefault();
      const id = draft.model_id.trim();
      if (!id || /[\s\x00-\x1f\x7f]/u.test(id)) { setError("请输入不含空白字符的真实 Model ID。"); return; }
      if (id !== model?.model_id && ids.has(id)) { setError("此 Model ID 已添加。"); return; }
      const { context_window: context, max_input_tokens: input, max_output_tokens: output, request_output_limit: budget } = draft;
      if ((context && ((input ?? 0) > context || (output ?? 0) > context || (budget ?? 0) >= context))
        || (output && (budget ?? 0) > output)) { setError("请核对上下文、输入长度、最大输出和请求预算的关系。"); return; }
      onSave({ ...draft, model_id: id, display_name: draft.display_name?.trim() }, makeDefault && draft.enabled !== false);
    }}>
      <label>真实 Model ID<input autoFocus required maxLength={128} value={draft.model_id}
        onChange={(event) => { setDraft({ ...draft, model_id: event.target.value }); setError(""); }} /></label>
      <label>显示名称<input maxLength={160} placeholder="留空时显示 Model ID" value={draft.display_name ?? ""}
        onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} /></label>
      <div className="catalog-inline">
        <label className="catalog-check"><input type="checkbox" checked={draft.enabled !== false}
          onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />启用模型</label>
        <label className="catalog-check"><input type="checkbox" disabled={defaultModel || !maySetDefault || draft.enabled === false}
          checked={draft.enabled !== false && makeDefault} onChange={(event) => setDefault(event.target.checked)} />设为默认模型</label>
      </div>
      <details className="catalog-advanced">
        <summary>高级参数与能力</summary>
        <p>规格可留空。请求预算只控制单次请求，能力声明由用户填写，尚未验证。</p>
        <div className="catalog-field-grid">
          {numericalFields.map(([field, label]) => <label key={field}>{label}
            <input type="number" min={field === "context_window" ? 2 : 1} max={2147483647} step={1}
              value={draft[field] ?? ""} onChange={(event) => {
                const value = event.target.valueAsNumber;
                setDraft({ ...draft, [field]: Number.isFinite(value) ? value : null, specification_source: "user" });
              }} />
          </label>)}
          {capabilityFields.map(([field, label]) => <label key={field}>{label}<select value={draft[field] ?? "unknown"}
            onChange={(event) => setDraft({ ...draft, [field]: event.target.value as Capability, specification_source: "user" })}>
            <option value="unknown">未知</option><option value="supported">声明支持</option><option value="unsupported">声明不支持</option>
          </select></label>)}
        </div>
        <p>规格来源 {draft.specification_source === "built_in" ? "内置文档"
          : draft.specification_source === "provider_catalog" ? "服务商目录"
            : draft.specification_source === "user" ? "用户配置" : "未知"}</p>
      </details>
      {error && <p className="catalog-error" role="alert">{error}</p>}
      <div className="catalog-actions">
        <button type="button" onClick={onClose}>取消</button>
        <button type="submit" className="primary">{model ? "应用修改" : "添加模型"}</button>
      </div>
    </form>
  </CatalogDialog>;
}
