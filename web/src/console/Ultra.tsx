import { useEffect, useState } from 'react';
import { api, type DelegationList } from '../api/client';

export function ExecutionModeLabel() {
  return <span className="execution-mode" aria-label="运行模式">Ultra · 多智能体</span>;
}

const labels: Record<string, string> = {
  intent: '意图已保存', submitting: '正在提交', unknown: '状态待核对', accepted: '已接收',
  working: '执行中', cancel_requested: '取消待确认', completed: '已完成', failed: '执行失败',
  cancelled: '已取消', rejected: '已拒绝',
};

export function DelegationPanel({ sessionId, mainStatus }: { sessionId: string; mainStatus: string }) {
  const [page, setPage] = useState<DelegationList | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    setPage(null); setError('');
    async function refresh() {
      try { const value = await api.delegations(sessionId); if (active) { setPage(value); setError(''); } }
      catch (cause) { if (active) setError(cause instanceof Error ? cause.message : String(cause)); }
      finally { if (active) timer = setTimeout(() => void refresh(), 3000); }
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [sessionId]);
  return <details className="ultra-panel" open>
    <summary>Ultra · 主 Agent：{mainStatus} · 子任务 {page?.tasks.length ?? '…'}</summary>
    <p className="fine">{page?.note ?? '新子任务可使用独立 Kali 工作区及本会话历史证据；历史任务保留原授权。暂停会取消子任务，不支持 Pi 断点续跑。'}</p>
    {error && <p role="alert">子任务状态读取失败：{error}</p>}
    {!error && page?.tasks.length === 0 && <p className="fine">尚未委派。主 Agent 可按需直接完成任务。</p>}
    {page?.tasks.map(task => <article key={task.id} className="delegation-item">
      <div><strong>{task.objective}</strong> <span className="tag">{labels[task.state] ?? task.state}</span></div>
      <small>委派 {task.id.slice(0, 8)} · Worker {task.worker_id?.slice(0, 8) ?? '待分配'} · A2A {task.a2a_task_id?.slice(0, 8) ?? '待核对'}</small>
      {task.summary && <p>{task.summary}</p>}
      {task.diagnostic && <p className="fine">{task.diagnostic}</p>}
      {task.result_artifact_id && <small>结果工件：{task.result_artifact_id}（低信任观察，非漏洞确认）</small>}
    </article>)}
  </details>;
}
