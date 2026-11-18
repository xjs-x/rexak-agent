import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider, type Viewport } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./graph.css";
import {
  ATTEMPT_OUTCOME_HINTS,
  OBJECT_KIND_LABELS,
  GRAPH_TITLES,
  RELATION_STATUS_HINTS,
  fieldLabel,
  outcomeLabel,
  parseSourceRef,
  statusLabel,
} from "./graph/graphLabels";
import { GraphCanvas, type LocateRequest } from "./graph/GraphCanvas";
import { GraphOverview } from "./graph/GraphOverview";
import { initialGraphSource, loadSessionGraph } from "./graph/graphSource";
import { buildGraphFlow, type FlowNode } from "./graph/graphLayout";
import type {
  AttemptEdge, AttackNode, GraphKey, InformationNode, RelationEdge, SessionGraphDocument,
} from "./graph/graphSchema";

type Resolved =
  | { kind: "entity"; object: InformationNode }
  | { kind: "relation"; object: RelationEdge }
  | { kind: "state"; object: AttackNode }
  | { kind: "attempt"; object: AttemptEdge };

function SourceRefList({ refs }: { refs: string[] }) {
  if (!refs.length) {
    return (
      <p className="gempty">
        尚无证据来源。该条目只由模型登记，缺少可追溯的回执，不能据此认定事实。
      </p>
    );
  }
  return (
    <ul className="gsources">
      {refs.map((ref) => {
        const parsed = parseSourceRef(ref);
        return (
          <li key={ref}>
            <span className={`gsrc-kind ${parsed.kind}`}>{parsed.label}</span>
            <code title={ref}>{parsed.uuid}</code>
          </li>
        );
      })}
    </ul>
  );
}

function FieldRow({ field, children }: { field: string; children: React.ReactNode }) {
  return (
    <div className="gfield">
      <span className="gk">{fieldLabel(field)}</span>
      <span className="gv">{children}</span>
    </div>
  );
}

function EntityDetail({
  document,
  entity,
  onSelectEdge,
}: {
  document: SessionGraphDocument;
  entity: InformationNode;
  onSelectEdge: (id: string) => void;
}) {
  const byId = new Map(document.information_graph.nodes.map((item) => [item.id, item]));
  const related = document.information_graph.edges.filter(
    (edge) => edge.from_id === entity.id || edge.to_id === entity.id,
  );
  return (
    <>
      <FieldRow field="label">{entity.label}</FieldRow>
      <FieldRow field="id">
        <code title={entity.id}>{entity.id}</code>
      </FieldRow>
      <h4 className="gsub">
        {fieldLabel("source_refs")}（{entity.source_refs.length}）
      </h4>
      <SourceRefList refs={entity.source_refs} />
      <h4 className="gsub">相关关系（{related.length}）</h4>
      {related.length ? (
        <ul className="grel">
          {related.map((edge) => {
            const outgoing = edge.from_id === entity.id;
            const loop = edge.from_id === edge.to_id;
            const other = byId.get(outgoing ? edge.to_id : edge.from_id);
            return (
              <li key={edge.id}>
                <button type="button" className="grel-btn" onClick={() => onSelectEdge(edge.id)}>
                  <span className="grel-dir">{loop ? "自环" : outgoing ? "指向" : "来自"}</span>
                  <span className="grel-other">{other?.label ?? "未解析的实体"}</span>
                  <span className={`gpill ${edge.status}`}>{statusLabel(edge.status)}</span>
                  <span className="grel-text">{edge.relation}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="gempty">该实体目前没有与其他实体的关系。</p>
      )}
      <p className="gnote">
        选择关联关系可查看端点、关系状态和来源。
      </p>
    </>
  );
}

function RelationDetail({
  document,
  edge,
  onOpenEntity,
}: {
  document: SessionGraphDocument;
  edge: RelationEdge;
  onOpenEntity: (id: string) => void;
}) {
  const byId = new Map(document.information_graph.nodes.map((item) => [item.id, item]));
  const from = byId.get(edge.from_id);
  const to = byId.get(edge.to_id);
  return (
    <>
      <FieldRow field="relation">{edge.relation}</FieldRow>
      <FieldRow field="status">
        <span className={`gpill ${edge.status}`}>{statusLabel(edge.status)}</span>
        <span className="gnote">{RELATION_STATUS_HINTS[edge.status]}</span>
      </FieldRow>
      <FieldRow field="from_id">
        <button type="button" className="glink" onClick={() => onOpenEntity(edge.from_id)}>
          {from?.label ?? "未解析的实体"}
        </button>
      </FieldRow>
      <FieldRow field="to_id">
        <button type="button" className="glink" onClick={() => onOpenEntity(edge.to_id)}>
          {to?.label ?? "未解析的实体"}
        </button>
      </FieldRow>
      <FieldRow field="id">
        <code title={edge.id}>{edge.id}</code>
      </FieldRow>
      <h4 className="gsub">
        {fieldLabel("source_refs")}（{edge.source_refs.length}）
      </h4>
      <SourceRefList refs={edge.source_refs} />
    </>
  );
}

function StateDetail({
  document,
  state,
  onOpenEntity,
  onSelectEdge,
}: {
  document: SessionGraphDocument;
  state: AttackNode;
  onOpenEntity: (id: string) => void;
  onSelectEdge: (id: string) => void;
}) {
  const byId = new Map(document.attack_graph.nodes.map((item) => [item.id, item]));
  const entityById = new Map(document.information_graph.nodes.map((item) => [item.id, item]));
  const related = document.attack_graph.edges.filter(
    (edge) => edge.from_id === state.id || edge.to_id === state.id,
  );
  return (
    <>
      <FieldRow field="summary">{state.summary}</FieldRow>
      <FieldRow field="id">
        <code title={state.id}>{state.id}</code>
      </FieldRow>
      <h4 className="gsub">关联实体（{state.entity_refs.length}）</h4>
      {state.entity_refs.length ? (
        <ul className="gentities">
          {state.entity_refs.map((ref) => {
            const entity = entityById.get(ref);
            return (
              <li key={ref}>
                <button type="button" className="glink" disabled={!entity} onClick={() => onOpenEntity(ref)}>
                  {entity?.label ?? "未解析的实体"}
                </button>
                <span className={`gflag ${entity && entity.source_refs.length ? "ok" : "warn"}`}>
                  {entity ? (entity.source_refs.length ? "有来源" : "无来源") : "缺失"}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="gempty">该状态没有关联任何实体。</p>
      )}
      <h4 className="gsub">相关尝试（{related.length}）</h4>
      {related.length ? (
        <ul className="grel">
          {related.map((edge) => {
            const outgoing = edge.from_id === state.id;
            const loop = edge.from_id === edge.to_id;
            const other = byId.get(outgoing ? edge.to_id : edge.from_id);
            return (
              <li key={edge.id}>
                <button type="button" className="grel-btn" onClick={() => onSelectEdge(edge.id)}>
                  <span className="grel-dir">{loop ? "自环" : outgoing ? "离开" : "进入"}</span>
                  <span className="grel-other">
                    {loop ? "原地重试" : other?.summary ?? "未解析的状态"}
                  </span>
                  <span className={`gpill ${edge.outcome}`}>{outcomeLabel(edge.outcome)}</span>
                  <span className="grel-text">{edge.action}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="gempty">该状态还没有记录任何尝试。</p>
      )}
      <p className="gnote">
        攻击状态本身不携带证据来源；证据挂在测试尝试上，请点开具体尝试查看。
      </p>
    </>
  );
}

function AttemptDetail({
  document,
  edge,
  onFocusState,
}: {
  document: SessionGraphDocument;
  edge: AttemptEdge;
  onFocusState: (id: string) => void;
}) {
  const byId = new Map(document.attack_graph.nodes.map((item) => [item.id, item]));
  const from = byId.get(edge.from_id);
  const to = byId.get(edge.to_id);
  const loop = edge.from_id === edge.to_id;
  return (
    <>
      <FieldRow field="action">{edge.action}</FieldRow>
      <FieldRow field="outcome">
        <span className={`gpill ${edge.outcome}`}>{outcomeLabel(edge.outcome)}</span>
        <span className="gnote">{ATTEMPT_OUTCOME_HINTS[edge.outcome]}</span>
      </FieldRow>
      <FieldRow field="from_id">
        <button type="button" className="glink" onClick={() => onFocusState(edge.from_id)}>
          {from?.summary ?? "未解析的状态"}
        </button>
      </FieldRow>
      <FieldRow field="to_id">
        <button type="button" className="glink" onClick={() => onFocusState(edge.to_id)}>
          {loop ? "同一状态（自环）" : to?.summary ?? "未解析的状态"}
        </button>
      </FieldRow>
      <FieldRow field="id">
        <code title={edge.id}>{edge.id}</code>
      </FieldRow>
      <h4 className="gsub">
        {fieldLabel("source_refs")}（{edge.source_refs.length}）
      </h4>
      <SourceRefList refs={edge.source_refs} />
    </>
  );
}

function SidePanel({
  document, resolved, onFocus, onSelectEdge, onOpenEntity,
}: {
  document: SessionGraphDocument;
  resolved: Resolved | null;
  onFocus: (id: string) => void;
  onSelectEdge: (id: string | null) => void;
  onOpenEntity: (id: string) => void;
}) {
  if (!resolved) return <p className="gside-empty">选择画布上的节点或连线查看详情。</p>;
  const selection = resolved.kind === "relation" || resolved.kind === "attempt";

  const kindLabel =
    resolved.kind === "entity" || resolved.kind === "relation"
      ? OBJECT_KIND_LABELS.information[resolved.kind === "entity" ? "node" : "edge"]
      : OBJECT_KIND_LABELS.attack[resolved.kind === "state" ? "node" : "edge"];

  const title =
    resolved.kind === "entity"
      ? resolved.object.label
      : resolved.kind === "relation"
        ? resolved.object.relation
        : resolved.kind === "state"
          ? resolved.object.summary
          : resolved.object.action;

  return (
    <div className="gside-body">
      <div className="gside-head">
        <span className={`gkind ${resolved.kind}`}>{kindLabel}</span>
        {selection ? (
          <button type="button" className="gclear" onClick={() => onSelectEdge(null)} title="清除边选择">
            清除选中
          </button>
        ) : null}
      </div>
      <h3 className="gside-title">{title}</h3>

      {resolved.kind === "entity" && (
        <EntityDetail document={document} entity={resolved.object} onSelectEdge={onSelectEdge} />
      )}
      {resolved.kind === "relation" && (
        <RelationDetail document={document} edge={resolved.object} onOpenEntity={onOpenEntity} />
      )}
      {resolved.kind === "state" && (
        <StateDetail
          document={document}
          state={resolved.object}
          onOpenEntity={onOpenEntity}
          onSelectEdge={onSelectEdge}
        />
      )}
      {resolved.kind === "attempt" && (
        <AttemptDetail document={document} edge={resolved.object} onFocusState={onFocus} />
      )}

      <details className="graw">
        <summary>技术字段（原始 JSON）</summary>
        <pre>{JSON.stringify(resolved.object, null, 2)}</pre>
      </details>
    </div>
  );
}

type View = GraphKey | "overview";
type Selected = { nodeId: string | null; edgeId: string | null };
type Navigation = { ids: string[]; at: number; edgeId: string | null };
const EMPTY_NAV: Navigation = { ids: [], at: -1, edgeId: null };
const SEARCH_PAGE = 40;

function GraphSearch({ nodes, onLocate }: { nodes: FlowNode[]; onLocate: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(SEARCH_PAGE);
  const [index, setIndex] = useState(0);
  const needle = query.trim().toLowerCase();
  const matches = useMemo(() => nodes.filter(node =>
    node.data.name.toLowerCase().includes(needle) || node.id.toLowerCase().includes(needle),
  ), [needle, nodes]);
  const visible = matches.slice(0, limit);

  function locate(id: string) {
    onLocate(id);
    setOpen(false);
  }

  return (
    <div className="gsearch" onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" aria-hidden="true">
        <circle cx="8.5" cy="8.5" r="4.8" strokeWidth="1.6" />
        <path d="m12 12 4 4" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
      <input
        type="search"
        role="combobox"
        aria-label="搜索节点名称或 ID"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls="session-graph-search"
        aria-activedescendant={open && visible[index] ? 'graph-result-' + index : undefined}
        placeholder="搜索节点名称或 ID…"
        value={query}
        onFocus={() => setOpen(true)}
        onChange={event => {
          setQuery(event.target.value);
          setOpen(true);
          setLimit(SEARCH_PAGE);
          setIndex(0);
        }}
        onKeyDown={event => {
          if (event.key === "Escape") setOpen(false);
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
            const next = Math.min(Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)), visible.length - 1);
            setIndex(Math.max(0, next));
            document.getElementById('graph-result-' + next)?.scrollIntoView?.({ block: "nearest" });
          }
          if (event.key === "Enter" && open && visible[index]) {
            event.preventDefault();
            locate(visible[index].id);
          }
        }}
      />
      {open && (
        <div className="gsearch-pop">
          <ul id="session-graph-search" role="listbox" aria-label="节点搜索结果">
            {visible.map((node, i) => (
              <li
                key={node.id}
                id={'graph-result-' + i}
                role="option"
                aria-selected={index === i}
                onMouseDown={event => event.preventDefault()}
                onClick={() => locate(node.id)}
                title={node.data.name}
              >
                <span>{node.data.name}</span>
                <code>{node.id}</code>
              </li>
            ))}
          </ul>
          {!matches.length && <p>没有匹配的节点</p>}
          {limit < matches.length && (
            <button type="button" onClick={() => setLimit(value => value + SEARCH_PAGE)}>
              显示更多（{visible.length} / {matches.length}）
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function GraphPage({ sessionId }: { sessionId: string | null }) {
  const [view, setView] = useState<View>("information");
  const [navigation, setNavigation] = useState<Record<GraphKey, Navigation>>({ information: EMPTY_NAV, attack: EMPTY_NAV });
  const [detailsOpen, setDetailsOpen] = useState(true);
  const [locate, setLocate] = useState<(LocateRequest & { graph: GraphKey }) | null>(null);
  const [source, setSource] = useState(() => initialGraphSource(sessionId));
  const [reload, setReload] = useState(0);
  const [fetching, setFetching] = useState(Boolean(sessionId));
  const [failure, setFailure] = useState<string | null>(null);
  const viewports = useRef<Partial<Record<GraphKey, Viewport>>>({});

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    setFetching(true);
    setFailure(null);
    async function load() {
      try {
        const result = await loadSessionGraph(sessionId, controller.signal);
        if (controller.signal.aborted) return;
        if (result.status === "error") setFailure(result.message);
        else setSource(result);
      } catch (error) {
        if (!controller.signal.aborted) setFailure(error instanceof Error ? error.message : "图数据读取失败");
      } finally {
        if (!controller.signal.aborted) setFetching(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [sessionId, reload]);

  const document = "document" in source ? source.document : null;
  const attackDisabled = "attackDisabled" in source && source.attackDisabled;
  const currentView = view === "attack" && attackDisabled ? "information" : view;
  const graph = currentView === "attack" ? "attack" : "information";
  const built = useMemo(() =>
    document && currentView !== "overview" ? buildGraphFlow(document, graph) : null,
  [currentView, document, graph]);
  const nav = navigation[graph];
  const activeId = nav.ids[nav.at];
  const selected: Selected = {
    nodeId: activeId && built?.nodeById.has(activeId) ? activeId : null,
    edgeId: nav.edgeId && built?.edgeById.has(nav.edgeId) ? nav.edgeId : null,
  };
  const node = selected.nodeId ? built?.nodeById.get(selected.nodeId)?.data.object : null;
  const edge = selected.edgeId ? built?.edgeById.get(selected.edgeId)?.data?.object : null;
  const resolved: Resolved | null = edge
    ? "relation" in edge ? { kind: "relation", object: edge } : { kind: "attempt", object: edge }
    : node ? "label" in node ? { kind: "entity", object: node } : { kind: "state", object: node } : null;

  function chooseNode(targetGraph: GraphKey, id: string, center = false) {
    setNavigation(previous => {
      const state = previous[targetGraph];
      if (state.ids[state.at] === id) {
        return state.edgeId ? { ...previous, [targetGraph]: { ...state, edgeId: null } } : previous;
      }
      const ids = [...state.ids.slice(0, state.at + 1), id].slice(-50);
      return { ...previous, [targetGraph]: { ids, at: ids.length - 1, edgeId: null } };
    });
    setDetailsOpen(true);
    if (center) setLocate({ graph: targetGraph, id });
  }

  function chooseEdge(id: string | null, targetGraph: GraphKey = graph) {
    setNavigation(previous => ({
      ...previous,
      [targetGraph]: { ...previous[targetGraph], edgeId: id },
    }));
    if (id) setDetailsOpen(true);
  }

  function history(at: number) {
    const id = nav.ids[at];
    if (!id) return;
    setNavigation(previous => ({ ...previous, [graph]: { ...previous[graph], at, edgeId: null } }));
    setLocate({ graph, id });
    setDetailsOpen(true);
  }

  const saveViewport = useCallback((viewport: Viewport) => {
    viewports.current[graph] = viewport;
  }, [graph]);
  const finishLocate = useCallback((request: LocateRequest) => {
    setLocate(current => current === request ? null : current);
  }, []);
  const refresh = () => setReload(value => value + 1);
  const badRecords = built ? built.invalidEdges + built.duplicateNodes + built.duplicateEdges : 0;

  return (
    <section className="view graph-view" aria-label="Session 双图" onKeyDown={event => {
      if (!built || !event.altKey || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
      event.preventDefault();
      history(nav.at + (event.key === "ArrowLeft" ? -1 : 1));
    }}>
      <div className="graph-head">
        <div className="graph-tabs" role="tablist" aria-label="图选择">
          {(["information", "attack", "overview"] as const).map(key => (
            <button
              key={key}
              role="tab"
              type="button"
              disabled={key === "attack" && attackDisabled}
              aria-selected={currentView === key}
              onClick={() => setView(key)}
            >
              {key === "overview" ? "概览" : GRAPH_TITLES[key]}
            </button>
          ))}
        </div>
        <div className="graph-meta">
          {source.status === "demo" && <span className="gmeta-item" title={source.reason}>演示数据</span>}
          {document && <span className="gmeta-item">版本 v{document.revision}</span>}
          <button type="button" className="gbutton" disabled={!sessionId || fetching} onClick={refresh}>
            {fetching ? "读取中…" : "刷新图谱"}
          </button>
        </div>
      </div>
      {failure && (
        <div className="gnotice" role="alert">
          <span>{failure}{document && " · 当前保留上次读取的图谱"}</span>
          <button type="button" className="gbutton" onClick={refresh}>重试</button>
        </div>
      )}
      {!document && !failure && <p className="gsource-state" role="status">正在读取图谱…</p>}
      {document && currentView === "overview" && (
        <GraphOverview document={document} onJump={(target, id, issueEdgeId) => {
          setView(target);
          chooseNode(target, id, true);
          if (issueEdgeId) chooseEdge(issueEdgeId, target);
        }} />
      )}
      {document && built && (
        <>
          <div className="gbar-tools">
            <GraphSearch key={graph} nodes={built.nodes} onLocate={id => chooseNode(graph, id, true)} />
            <div className="gnav" role="group" aria-label="浏览历史">
              <button type="button" aria-label="后退" title="后退" onClick={() => history(nav.at - 1)} disabled={nav.at < 1}>←</button>
              <button type="button" aria-label="前进" title="前进" onClick={() => history(nav.at + 1)} disabled={nav.at >= nav.ids.length - 1}>→</button>
            </div>
            <span className="gcounts">
              {built.nodes.length} 节点 · {built.edges.length} 边 · {built.components} 分量
            </span>
            <button type="button" className="gbutton gdetails-toggle" aria-expanded={detailsOpen} onClick={() => setDetailsOpen(value => !value)}>
              {detailsOpen ? "收起详情" : "展开详情"}
            </button>
          </div>
          {badRecords > 0 && (
            <p className="gnotice" role="status">
              图文档包含 {built.invalidEdges} 条缺少端点的边、{built.duplicateNodes} 个重复节点和 {built.duplicateEdges} 条重复边。画布已保留有效记录。
            </p>
          )}
          <div className="graph-body">
            {built.nodes.length ? (
              <ReactFlowProvider key={graph}>
                <GraphCanvas
                  built={built}
                  graph={graph}
                  nodeId={selected.nodeId}
                  edgeId={selected.edgeId}
                  locate={locate?.graph === graph ? locate : null}
                  viewport={viewports.current[graph]}
                  onViewport={saveViewport}
                  onNode={id => chooseNode(graph, id)}
                  onEdge={chooseEdge}
                  onLocated={finishLocate}
                />
              </ReactFlowProvider>
            ) : (
              <div className="gsource-state" role="status">当前{GRAPH_TITLES[graph]}暂无节点。</div>
            )}
            {detailsOpen && (
              <aside className="gside" aria-label="图谱详情" data-has-selection={Boolean(resolved)}>
                <header>
                  <strong>详情</strong>
                  <button type="button" className="gbutton" aria-label="关闭详情" onClick={() => setDetailsOpen(false)}>×</button>
                </header>
                <SidePanel
                  document={document}
                  resolved={resolved}
                  onFocus={id => chooseNode(graph, id, true)}
                  onSelectEdge={chooseEdge}
                  onOpenEntity={id => {
                    setView("information");
                    chooseNode("information", id, true);
                  }}
                />
              </aside>
            )}
          </div>
        </>
      )}
      <footer className="graph-foot">
        {source.status === "demo" ? "演示数据 · " : ""}
        图谱记录包含模型判断，来源引用可用于核对证据。
      </footer>
    </section>
  );
}

export function SessionGraph({ sessionId = null }: { sessionId?: string | null }) {
  return <GraphPage key={sessionId ?? "__demo__"} sessionId={sessionId} />;
}
