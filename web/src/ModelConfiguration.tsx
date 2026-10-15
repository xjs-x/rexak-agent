import { useState } from "react";
import { api, type ModelSettings } from "./api/client";

export function ModelConfiguration({ settings, onSaved }: {
  settings: ModelSettings | null; onSaved: (settings: ModelSettings) => void;
}) {
  const [selected, setSelected] = useState("");
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [makeDefault, setMakeDefault] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const profile = settings?.profiles.find((item) => `${item.provider_id}/${item.model_name}` === selected)
    ?? settings?.profiles.find((item) => item.provider_id === settings.default_provider && item.model_name === settings.default_model);
  const save = async (remove = false) => {
    if (!profile) return;
    setBusy(true); setNotice("");
    try {
      const updated = await api.saveModelSettings({
        provider_id: profile.provider_id, model_name: profile.model_name,
        base_url: endpoint ?? profile.base_url,
        ...(key.trim() && !remove ? { api_key: key.trim() } : {}),
        delete_api_key: remove, set_default: makeDefault,
      });
      setKey(""); setEndpoint(null); onSaved(updated);
      setNotice(remove ? "已删除本机保存的 Key。" : "已保存，新的运行阶段、暂停后继续及下次启动会读取此配置。");
    } catch (cause) { setNotice(String(cause)); }
    finally { setBusy(false); }
  };
  if (!settings || !profile) return <p role="status">读取模型配置中…</p>;
  return <section className="model-configuration">
    <p className="fineprint">{settings.effective_scope} 同一供应商的模型共用 Key。</p>
    <label>配置模型<select aria-label="配置模型" value={`${profile.provider_id}/${profile.model_name}`} disabled={busy} onChange={(event) => { setSelected(event.target.value); setKey(""); setEndpoint(null); setNotice(""); }}>
      {settings.profiles.map((item) => <option key={`${item.provider_id}/${item.model_name}`} value={`${item.provider_id}/${item.model_name}`}>{item.provider_id === "xiaomi" ? "小米" : "百炼"} · {item.model_name}</option>)}
    </select></label>
    <label>OpenAI 兼容 Base URL<input aria-label="模型 Base URL" value={endpoint ?? profile.base_url} disabled={busy} onChange={(event) => setEndpoint(event.target.value)} placeholder="供应商控制台的 HTTPS 接入地址" /></label>
    <label>API Key<input aria-label="模型 API Key" type="password" autoComplete="new-password" value={key} disabled={busy} onChange={(event) => setKey(event.target.value)} placeholder={profile.secret_configured ? "已配置；留空保留，输入新 Key 可替换" : "输入一次并保存到本机"} /></label>
    <p className="fineprint">Windows 使用当前用户的系统加密存储。Key 不会回显，也不会下发 Kali。</p>
    <dl className="model-limits"><dt>上下文窗口</dt><dd>{profile.context_limit.toLocaleString()} tokens</dd><dt>请求输出额度（含思考）</dt><dd>{profile.request_output_limit.toLocaleString()} tokens</dd><dt>可用输入预算（含安全余量）</dt><dd>{profile.allowed_input_tokens.toLocaleString()} tokens</dd></dl>
    <label className="checkbox-row"><input type="checkbox" checked={makeDefault} onChange={(event) => setMakeDefault(event.target.checked)} />设为新阶段的默认模型</label>
    <div className="model-settings-actions"><button disabled={busy} onClick={() => void save()}>保存模型设置</button><button disabled={busy || !profile.secret_configured} onClick={() => void save(true)}>删除已保存 Key</button><span className={`pill ${profile.secret_configured ? "status-confirmed" : "status-blocked"}`}>{profile.secret_configured ? "Key 已配置" : "尚未配置 Key"}</span></div>
    {notice && <p role="status">{notice}</p>}
  </section>;
}
