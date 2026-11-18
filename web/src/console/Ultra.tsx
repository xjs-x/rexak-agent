import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api, type DelegationList } from '../api/client';
import type { components } from '../api/generated/schema';
import { Icon } from './Icon';
import { assistantDisplayText, isInternalAssistantContent } from './assistantDisplay';
import { MarkdownText } from './Markdown';

export function ExecutionModeLabel() {
  return <span className="execution-mode" aria-label="运行模式">Ultra · 多智能体</span>;
}

const labels: Record<string, string> = {
  intent: '意图已保存', submitting: '正在提交', unknown: '状态待核对', accepted: '已接收',
  working: '执行中', cancel_requested: '取消待确认', completed: '已完成', failed: '执行失败',
  cancelled: '已取消', rejected: '已拒绝',
};

export function DelegationPanel({ sessionId, mainStatus, onOpen }: { sessionId: string; mainStatus: string; onOpen?: (id: string) => void }) {
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
      {onOpen && <button type="button" onClick={() => onOpen(task.id)}>查看 Worker 轨迹</button>}
    </article>)}
  </details>;
}

type VisibleEvent = components['schemas']['WorkerVisibleEvent'];
type Finding = components['schemas']['WorkerTrajectoryFinding'];
const toolStates: Record<string, string> = { running: '执行中', callback_completed: '工具已返回', failed: '工具失败' };

function eventText(event: VisibleEvent): string {
  const value = event.message;
  if (isInternalAssistantContent(event) || isInternalAssistantContent(value)) return '';
  const content = value.content;
  let text = '';
  if (Array.isArray(content)) {
    text = content.filter((part): part is { type: 'text'; text: string } =>
      !!part && typeof part === 'object' && part.type === 'text' &&
      typeof part.text === 'string' && !isInternalAssistantContent(part))
      .map(part => part.text).join('\n');
  } else if (typeof content === 'string') text = content;
  else if (typeof value.preview === 'string') text = value.preview;
  else if (content && typeof content === 'object' && 'preview' in content &&
           typeof content.preview === 'string') text = content.preview;
  return event.role === 'assistant' ? assistantDisplayText(text) : text;
}

function eventLabel(event: VisibleEvent) {
  if (event.role === 'user') return '委派输入';
  if (event.role === 'tool_execution') return `工具 · ${String(event.message.toolName ?? '')}`;
  if (event.role === 'toolResult') return `工具结果 · ${String(event.message.toolName ?? '')}`;
  return event.role === 'assistant' ? 'Worker' : event.role;
}

type WorkerPanelProps = {
  sessionId: string; delegationId: string; onSelect: (id: string) => void; onClose: () => void;
};
type SavedHistory = { events: VisibleEvent[]; cursor: number; hasMore: boolean; position?: number };

export function WorkerPanel(props: WorkerPanelProps) {
  const history = useRef(new Map<string, SavedHistory>());
  const key = `${props.sessionId}:${props.delegationId}`;
  return <WorkerTrajectory key={key} {...props} history={history.current} historyKey={key} />;
}

function WorkerTrajectory({ sessionId, delegationId, onSelect, onClose, history, historyKey }: WorkerPanelProps & {
  history: Map<string, SavedHistory>; historyKey: string;
}) {
  const saved = useRef(history.get(historyKey));
  const [tasks, setTasks] = useState<DelegationList['tasks']>([]);
  const [events, setEvents] = useState<VisibleEvent[]>(saved.current?.events ?? []);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [resumeFrom, setResumeFrom] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(saved.current?.hasMore ?? false);
  const [cursor, setCursor] = useState(saved.current?.cursor ?? 0);
  const [incomplete, setIncomplete] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<Record<number, VisibleEvent>>({});
  const [outputs, setOutputs] = useState<Record<string, { content: string; next: number | null }>>({});
  const [streamState, setStreamState] = useState('连接中');
  const [now, setNow] = useState(Date.now());
  const [atBottom, setAtBottom] = useState(saved.current?.position == null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const prepend = useRef<{ height: number; top: number } | null>(null);
  const position = useRef(saved.current?.position);
  const task = tasks.find(item => item.id === delegationId);
  const terminal = ['completed', 'failed', 'cancelled', 'rejected'].includes(task?.state ?? '');
  const elapsedEnd = terminal && task?.updated_at ? Date.parse(task.updated_at) : now;
  const objective = task?.objective ?? '正在读取委派目标';
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try { const list = await api.delegations(sessionId); if (active) setTasks(list.tasks); }
      catch (cause) { if (active) setError(String(cause)); }
      finally { if (active) timer = setTimeout(() => void refresh(), 3000); }
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [sessionId]);

  useEffect(() => {
    let active = true;
    let source: EventSource | undefined;
    const url = `/sessions/${sessionId}/delegations/${delegationId}`;
    const connect = (after?: string | null) => {
      source = new EventSource(`/api${url}/events${after ? `?after=${encodeURIComponent(after)}` : ''}`);
      source.onopen = () => { if (active) setStreamState('实时连接'); };
      source.onerror = () => { if (active) setStreamState('断线重连中'); };
      source.onmessage = message => {
        if (!active) return;
        try { mergeEvents([JSON.parse(message.data) as VisibleEvent]); }
        catch { setError('Worker 实时事件解析失败'); }
      };
    };
    void api.workerTrajectory(sessionId, delegationId, { before: Number.MAX_SAFE_INTEGER }).then(value => {
      if (!active) return;
      const cached = saved.current?.events.at(-1);
      const gap = cached && value.events[0] && value.events[0].sequence > cached.sequence + 1;
      if (gap) {
        setEvents(value.events);
        position.current = undefined;
        setAtBottom(true);
      } else mergeEvents(value.events);
      setFindings(value.findings);
      setResumeFrom(value.resume_from_delegation_id ?? null);
      if (gap || !saved.current?.cursor) { setHasMore(value.has_more); setCursor(value.next_cursor); }
      setIncomplete(value.history_incomplete);
      connect(value.stream_cursor);
    }).catch(cause => { if (active) { setError(String(cause)); connect(); } });
    const findingTimer = window.setInterval(() => void api.workerTrajectory(sessionId, delegationId, { after: Number.MAX_SAFE_INTEGER })
      .then(value => { if (active) setFindings(value.findings); })
      .catch(cause => { if (active) setError(`重要发现更新失败：${String(cause)}`); }), 3000);
    return () => {
      active = false; source?.close(); window.clearInterval(findingTimer);
    };
  }, [sessionId, delegationId]);

  function mergeEvents(incoming: VisibleEvent[]) {
    setEvents(previous => {
      const map = new Map(previous.map(item => [item.sequence, item]));
      let changed = false;
      for (const item of incoming) {
        if ((map.get(item.sequence)?.revision ?? -1) >= item.revision) continue;
        map.set(item.sequence, item);
        changed = true;
      }
      return changed ? [...map.values()].sort((a, b) => a.sequence - b.sequence) : previous;
    });
  }

  useLayoutEffect(() => {
    if (scroll.current && saved.current?.position != null) scroll.current.scrollTop = saved.current.position;
  }, []);
  useLayoutEffect(() => {
    const element = scroll.current;
    if (!element) return;
    if (prepend.current) {
      element.scrollTop = prepend.current.top + element.scrollHeight - prepend.current.height;
      prepend.current = null;
    } else if (atBottom) element.scrollTop = element.scrollHeight;
  }, [events, findings, atBottom, loadingHistory]);
  useEffect(() => {
    return () => {
      if (events.length) history.set(historyKey, { events, cursor, hasMore, position: position.current });
    };
  }, [events, cursor, hasMore, history, historyKey]);

  async function loadOlder() {
    setLoadingHistory(true);
    try {
      const result = await api.workerTrajectory(sessionId, delegationId, { before: cursor });
      if (scroll.current) prepend.current = { height: scroll.current.scrollHeight, top: scroll.current.scrollTop };
      mergeEvents(result.events);
      setCursor(result.next_cursor);
      setHasMore(result.has_more);
    } catch (cause) { setError(String(cause)); }
    finally { setLoadingHistory(false); }
  }

  const visibleEvents = events.filter(event =>
    !isInternalAssistantContent(event) && !isInternalAssistantContent(event.message));
  const activityAt = [...visibleEvents.map(event => event.updated_at), ...findings.map(finding => finding.at)]
    .reduce((latest, at) => Math.max(latest, Date.parse(at) || 0), 0);
  const timeline = [
    ...visibleEvents.map(event => ({ at: event.at, key: `event-${event.sequence}`, event, finding: null as Finding | null })),
    ...findings.map(finding => ({ at: finding.at, key: `finding-${finding.id}`, finding, event: null as VisibleEvent | null })),
  ].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const readOutput = (artifactId: string, offset = 0) => void api.workerOutput(sessionId, delegationId, artifactId, offset)
    .then(value => setOutputs(previous => ({ ...previous, [artifactId]: {
      content: (offset ? previous[artifactId]?.content ?? '' : '') + value.content, next: value.next_offset,
    } }))).catch(cause => setError(`Worker 输出读取失败：${String(cause)}`));
  return <aside className="worker-panel" aria-label="Worker 运行轨迹">
    <header className="worker-head">
      <div><span className="worker-identity"><Icon name="layers" /> <strong>Worker {task?.worker_id?.slice(0, 8) ?? delegationId.slice(0, 8)}</strong></span>
        <span className="tag">{labels[task?.state ?? ''] ?? task?.state ?? '读取中'}</span>
        <button type="button" className="icon-btn" aria-label="关闭 Worker 面板" onClick={onClose}><Icon name="close" /></button>
      </div>
      <details className="worker-objective"><summary>任务目标 · {objective}</summary><p>{objective}</p></details>
      <small>{streamState} · {task?.created_at ? `${terminal ? '运行用时' : '已运行'} ${Math.max(0, Math.floor((elapsedEnd - Date.parse(task.created_at)) / 1000))} 秒` : ''}</small>
      {activityAt > 0 && <small>最近活动 · {new Date(activityAt).toLocaleTimeString()}</small>}
      <select aria-label="切换 Worker" value={delegationId} onChange={e => onSelect(e.target.value)}>
        {tasks.map(item => <option key={item.id} value={item.id}>{item.worker_id?.slice(0, 8) ?? item.id.slice(0, 8)} · {labels[item.state] ?? item.state}</option>)}
      </select>
      {resumeFrom && <button type="button" onClick={() => onSelect(resumeFrom)}>续接自 Worker {resumeFrom.slice(0, 8)}</button>}
      <details className="worker-findings"><summary>重要发现 · {findings.length}</summary>
        {findings.map(item => <div key={item.id} className="worker-finding-body">
          <p>{item.summary}</p>
          <small>{new Date(item.at).toLocaleString()} · {item.delivery_status}</small>
          {Array.isArray(item.evidence_refs) && [...new Set(item.evidence_refs.filter(ref => typeof ref === 'string'))].map(ref =>
            <code key={ref}>{ref}</code>)}
        </div>)}
        {findings.length === 0 && <p className="fine">暂未收到发现。</p>}
      </details>
    </header>
    <div className="worker-scroll" ref={scroll} onScroll={e => {
      const element = e.currentTarget;
      const bottom = element.scrollHeight - element.scrollTop - element.clientHeight < 64;
      position.current = bottom ? undefined : element.scrollTop;
      setAtBottom(bottom);
    }}>
      {error && <p role="alert">{error}</p>}
      {incomplete && <p className="fine">旧委派没有完整可见历史；以下只展示已保存记录。</p>}
      {!error && !incomplete && !timeline.length && !task?.summary && <p className="fine">
        {terminal ? '此委派没有已保存的运行记录。' : '等待 Worker 的可见运行记录…'}
      </p>}
      {hasMore && <button disabled={loadingHistory} onClick={() => void loadOlder()}>{loadingHistory ? '正在读取…' : '加载更早记录'}</button>}
      {timeline.map(item => item.finding ? <article className="worker-event finding" key={item.key}>
        <strong>重要发现 · 候选</strong><p>{item.finding.summary}</p><small>{item.finding.delivery_status} · {new Date(item.at).toLocaleString()}</small>
      </article> : item.event && (() => {
        const event = item.event;
        const loaded = expanded[event.sequence];
        const full = loaded?.revision === event.revision ? loaded : undefined;
        const body = eventText(full ?? event);
        return <article className="worker-event" key={item.key}>
          <div className="worker-event-title"><strong>{eventLabel(event)}</strong>
            <small>{event.phase === 'streaming' ? terminal ? '中断或状态未知' : '执行中' :
              event.role === 'tool_execution' ? toolStates[String(event.message.status)] ?? '已记录' :
              new Date(item.at).toLocaleTimeString()}</small></div>
          {body && <MarkdownText text={body} />}
          {event.message.body_unavailable === true && <p className="fine">完整正文未能归档，已保留预览。</p>}
          {event.role === 'tool_execution' && <pre>{
            JSON.stringify((full ?? event).message.args ?? {}, null, 2)
          }</pre>}
          {event.role === 'toolResult' && Array.isArray(event.message.artifactRefs) &&
            event.message.artifactRefs.map((ref, index) => {
              if (!ref || typeof ref !== 'object' || typeof (ref as { artifact_id?: unknown }).artifact_id !== 'string') return null;
              const artifactId = (ref as { artifact_id: string }).artifact_id;
              const output = outputs[artifactId];
              return <div key={`${artifactId}-${index}`}>
                <button type="button" onClick={() => readOutput(artifactId)}>查看完整工具输出 {artifactId.slice(0, 8)}</button>
                {output && <><pre>{output.content}</pre>
                  {output.next !== null && <button type="button" onClick={() => readOutput(artifactId, output.next!)}>读取后续输出</button>}</>}
              </div>;
            })}
          {event.message.truncated === true && !full &&
            <button onClick={() => void api.workerTrajectoryMessage(sessionId, delegationId, event.sequence)
            .then(value => setExpanded(old => ({ ...old, [value.sequence]: value })))
            .catch(cause => setError(String(cause)))}>查看完整内容</button>}
        </article>;
      })())}
      {task?.summary && <article className="worker-event"><strong>最终结果</strong><p>{task.summary}</p></article>}
    </div>
    {!atBottom && <button className="worker-latest" onClick={() => {
      position.current = undefined;
      setAtBottom(true);
    }}>查看最新消息</button>}
  </aside>;
}
