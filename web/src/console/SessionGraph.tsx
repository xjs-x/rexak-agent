/** Session 双图只读视图：读取真实 API，支持手动刷新与对象详情。 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./graph.css";
import {
  ATTEMPT_OUTCOME_HINTS,
  ATTEMPT_OUTCOME_LABELS,
  FIELD_LABELS,
  GRAPH_CAPTIONS,
  GRAPH_TITLES,
  OBJECT_KIND_LABELS,
  OVERVIEW_CAPTION,
  OVERVIEW_TITLE,
  RELATION_STATUS_HINTS,
  RELATION_STATUS_LABELS,
  fieldLabel,
  outcomeLabel,
  parseSourceRef,
  shortId,
  statusLabel,
} from "./graph/graphLabels";
import { GRAPH_EDGE_TYPES } from "./graph/GraphEdges";
import { GRAPH_NODE_TYPES } from "./graph/GraphNodes";
import { GraphOverview } from "./graph/GraphOverview";
import { initialGraphSource, loadSessionGraph, type GraphSource } from "./graph/graphSource";
import {
  buildAttackFlow,
  buildInformationFlow,
  FOLD_ID,
  WING_LIMIT,
  type BuiltFlow,
  type HiddenItem,
  type Selection,
} from "./graph/graphLayout";
import type {
  AttemptEdge,
  AttackNode,
  GraphKey,
  InformationNode,
  RelationEdge,
  SessionGraphDocument,
} from "./graph/graphSchema";

const OUTCOME_ORDER = ["success", "negative", "blocked", "unknown"] as const;
const STATUS_ORDER = ["observed", "hypothesis", "refuted", "superseded"] as const;

/** 图内对象查找结果；`kind` 决定详情面板渲染哪一组字段。 */
type Resolved =
  | { kind: "entity"; object: InformationNode }
  | { kind: "relation"; object: RelationEdge }
  | { kind: "state"; object: AttackNode }
  | { kind: "attempt"; object: AttemptEdge }
  | null;

/** 详情面板当前展示什么：折叠清单优先于连线，连线优先于焦点对象。 */
type Panel = { kind: "fold"; side: "inbound" | "outbound" } | null;

function resolve(
  document: SessionGraphDocument,
  graph: GraphKey,
  focusId: string | null,
  selection: Selection,
): Resolved {
  if (selection) {
    if (graph === "information") {
      const object = document.information_graph.edges.find((item) => item.id === selection.id);
      return object ? { kind: "relation", object } : null;
    }
    const object = document.attack_graph.edges.find((item) => item.id === selection.id);
    return object ? { kind: "attempt", object } : null;
  }
  if (!focusId) return null;
  if (graph === "information") {
    const object = document.information_graph.nodes.find((item) => item.id === focusId);
    return object ? { kind: "entity", object } : null;
  }
  const object = document.attack_graph.nodes.find((item) => item.id === focusId);
  return object ? { kind: "state", object } : null;
}

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
            <code title={ref}>{shortId(parsed.uuid, 8)}</code>
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
            const other = byId.get(outgoing ? edge.to_id : edge.from_id);
            return (
              <li key={edge.id}>
                <button type="button" className="grel-btn" onClick={() => onSelectEdge(edge.id)}>
                  <span className="grel-dir">{outgoing ? "指向" : "来自"}</span>
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
        点击“相关关系”里的条目会选中对应连线；点击上方当前焦点可以换成别的实体。
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
                <button type="button" className="glink" onClick={() => onOpenEntity(ref)}>
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

/**
 * 折叠清单：画布上放不下的邻居在这里列全。
 * 它存在的意义就是「图可以一直变大，但每一屏都可控」。
 */
function FoldDetail({
  graph,
  side,
  items,
  total,
  onFocus,
  onClose,
}: {
  graph: GraphKey;
  side: "inbound" | "outbound";
  items: HiddenItem[];
  total: number;
  onFocus: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <>
      <div className="gside-head">
        <span className="gkind fold">{side === "inbound" ? "折叠的来源" : "折叠的去向"}</span>
        <button type="button" className="gclear" onClick={onClose}>
          返回焦点
        </button>
      </div>
      <h3 className="gside-title">
        {side === "inbound" ? "还有更多对象指向它" : "它还指向更多对象"}
      </h3>
      <p className="gnote" style={{ marginTop: 0 }}>
        画布每侧最多画 {WING_LIMIT} 个，这一侧共 {total} 个。下面是被收起来的 {items.length} 个，
        点任意一条可以把它换成当前焦点。
      </p>
      <ul className="grel">
        {items.map((item) => (
          <li key={item.id}>
            <button type="button" className="grel-btn" onClick={() => onFocus(item.id)}>
              <span className="grel-dir">{graph === "information" ? "实体" : "状态"}</span>
              <span className="grel-other">{item.name}</span>
              <span className="grel-text">{shortId(item.id, 8)}</span>
            </button>
          </li>
        ))}
      </ul>
      {!items.length && <p className="gempty">这一侧没有被折叠的对象。</p>}
    </>
  );
}

function Legend({ graph }: { graph: GraphKey }) {
  if (graph === "information") {
    return (
      <div className="glegend" aria-label="图例">
        {STATUS_ORDER.map((status) => (
          <span key={status}>
            <i className={`gline ${status}`} />
            {RELATION_STATUS_LABELS[status]}
          </span>
        ))}
        <span>
          <i className="gnode-dot ok" />
          有证据来源
        </span>
        <span>
          <i className="gnode-dot warn" />
          尚无证据来源
        </span>
      </div>
    );
  }
  return (
    <div className="glegend" aria-label="图例">
      {OUTCOME_ORDER.map((outcome) => (
        <span key={outcome}>
          <i className={`gline ${outcome}`} />
          {ATTEMPT_OUTCOME_LABELS[outcome]}
        </span>
      ))}
      <span>
        <i className="gline loop" />
        自环：同一状态上再次尝试
      </span>
    </div>
  );
}

function Toolbar({ onReset }: { onReset: () => void }) {
  const { zoomIn, zoomOut } = useReactFlow();
  return (
    <div className="gtools">
      <button type="button" className="zoom" onClick={() => void zoomOut({ duration: 160 })} title="缩小">
        −
      </button>
      <button type="button" className="zoom" onClick={() => void zoomIn({ duration: 160 })} title="放大">
        +
      </button>
      <button type="button" onClick={onReset} title="适应画布">
        适应画布
      </button>
    </div>
  );
}

function Canvas({
  document,
  graph,
  focusId,
  selection,
  onFocus,
  onSelectEdge,
  onOpenFold,
}: {
  document: SessionGraphDocument;
  graph: GraphKey;
  focusId: string | null;
  selection: Selection;
  onFocus: (id: string) => void;
  onSelectEdge: (id: string | null) => void;
  onOpenFold: (side: "inbound" | "outbound") => void;
}) {
  const { fitView } = useReactFlow();
  const built = useMemo(
    () =>
      graph === "information"
        ? buildInformationFlow(document, focusId, selection)
        : buildAttackFlow(document, focusId, selection),
    [document, graph, focusId, selection],
  );

  // 切换焦点会改变节点数量和位置，必须重新适配；否则新焦点的一翼会溢出画布。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void fitView({ padding: 0.12, maxZoom: 1.05, duration: 0 });
    }, 60);
    return () => window.clearTimeout(timer);
  }, [focusId, graph, fitView]);

  return (
    <div className="gcanvas">
      <ReactFlow
        key={graph}
        nodes={built.nodes as Node[]}
        edges={built.edges as Edge[]}
        nodeTypes={GRAPH_NODE_TYPES}
        edgeTypes={GRAPH_EDGE_TYPES}
        onNodeClick={(_, node) => {
          if (node.id === FOLD_ID.inbound) {
            onSelectEdge(null);
            onOpenFold("inbound");
            return;
          }
          if (node.id === FOLD_ID.outbound) {
            onSelectEdge(null);
            onOpenFold("outbound");
            return;
          }
          onSelectEdge(null);
          onFocus(node.id);
        }}
        onEdgeClick={(_, edge) => onSelectEdge(edge.id)}
        onPaneClick={() => onSelectEdge(null)}
        fitView
        fitViewOptions={{ padding: 0.12, maxZoom: 1.05 }}
        minZoom={0.3}
        maxZoom={1.8}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        panOnScroll
        selectionOnDrag={false}
        className="gflow"
      >
        <Background color="#e3e3e6" gap={22} size={1} />
      </ReactFlow>
      <Toolbar onReset={() => void fitView({ padding: 0.12, maxZoom: 1.05, duration: 260 })} />
      <Legend graph={graph} />
      {built.dangling > 0 && (
        <p className="gdangling" role="status">
          有 {built.dangling} 条连线引用了不存在的节点，已从画布隐藏。后端写入时会拒绝这类引用，
          此处说明读取到的文档本身不一致。
        </p>
      )}
    </div>
  );
}

/**
 * 焦点选择器：按钮 + 可搜索下拉。
 * 对象上百时，横向药丸条既放不下也翻不完，所以改成「搜 + 列表」。
 */
function FocusPicker({
  label,
  items,
  value,
  onChange,
}: {
  label: string;
  items: { id: string; name: string; meta: string }[];
  value: string | null;
  onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const current = items.find((item) => item.id === value);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as globalThis.Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) => item.name.toLowerCase().includes(needle));
  }, [items, query]);

  return (
    <div className="gpicker" ref={box}>
      <button
        type="button"
        className="gpicker-btn"
        aria-expanded={open}
        aria-label={`选择${label}`}
        onClick={() => {
          setOpen(!open);
          setQuery("");
        }}
      >
        <span className="gpicker-name">{current?.name ?? `选择${label}`}</span>
        <span className="gpicker-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <div className="gpicker-pop" role="dialog" aria-label={`选择${label}`}>
          <input
            autoFocus
            className="gpicker-search"
            value={query}
            placeholder={`搜索${label}…`}
            onChange={(event) => setQuery(event.target.value)}
          />
          <ul className="gpicker-list">
            {filtered.slice(0, 200).map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className={`gpicker-item${item.id === value ? " on" : ""}`}
                  onClick={() => {
                    onChange(item.id);
                    setOpen(false);
                  }}
                >
                  <span className="gpicker-item-name">{item.name}</span>
                  <span className="gpicker-item-meta">{item.meta}</span>
                </button>
              </li>
            ))}
            {!filtered.length && <li className="gpicker-empty">没有匹配的对象</li>}
          </ul>
          <p className="gpicker-foot">
            共 {items.length} 个 · 当前显示 {Math.min(filtered.length, 200)} 个
          </p>
        </div>
      )}
    </div>
  );
}

function SidePanel({
  document,
  graph,
  panel,
  selection,
  resolved,
  hidden,
  counts,
  onFocus,
  onSelectEdge,
  onOpenEntity,
  onClosePanel,
}: {
  document: SessionGraphDocument;
  graph: GraphKey;
  panel: Panel;
  selection: Selection;
  resolved: Resolved;
  hidden: { inbound: HiddenItem[]; outbound: HiddenItem[] };
  counts: { inbound: number; outbound: number };
  onFocus: (id: string) => void;
  onSelectEdge: (id: string | null) => void;
  onOpenEntity: (id: string) => void;
  onClosePanel: () => void;
}) {
  if (panel?.kind === "fold") {
    return (
      <div className="gside-body">
        <FoldDetail
          graph={graph}
          side={panel.side}
          items={hidden[panel.side]}
          total={counts[panel.side]}
          onFocus={onFocus}
          onClose={onClosePanel}
        />
      </div>
    );
  }

  if (!resolved) {
    return (
      <div className="gside-body">
        <h3 className="gside-title">详情</h3>
        <p className="gempty">当前文档里没有可展示的对象。</p>
      </div>
    );
  }

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
          <button type="button" className="gclear" onClick={() => onSelectEdge(null)} title="回到焦点对象">
            返回焦点
          </button>
        ) : (
          <span className="gclear-hint">画布中心</span>
        )}
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

/**
 * 浏览历史。`ids` 是一条时间线，`at` 是当前位置；
 * 后退只是把 `at` 往回挪，前进再挪回来，选新对象则截断后面的分支。
 * 上限 50 条，避免长时间点选把内存堆满。
 */
const HISTORY_LIMIT = 50;

type Nav = { ids: string[]; at: number };

/**
 * 数据源还没就绪时的三态：加载中 / 读取失败 / 文档为空。
 * 都在页面主体区，不遮挡顶部的标签与说明。
 */
function SourceState({ source, onRetry }: { source: GraphSource; onRetry: () => void }) {
  if (source.status === "loading") {
    return (
      <div className="gsource-state" role="status" aria-live="polite">
        <span className="gspinner" aria-hidden="true" />
        <p>{source.reason}</p>
      </div>
    );
  }
  if (source.status === "error") {
    return (
      <div className="gsource-state" role="alert">
        <h3>图数据没有读出来</h3>
        <p>{source.message}</p>
        <button type="button" className="mini-btn" onClick={onRetry}>
          重试
        </button>
      </div>
    );
  }
  if (source.status === "empty") {
    return (
      <div className="gsource-state">
        <h3>还没有图数据</h3>
        <p>{source.reason}</p>
        <p className="gnote">
          模型还没有在这个会话里登记任何实体或攻击状态。这不代表目标没有问题。
        </p>
      </div>
    );
  }
  return null;
}

/** `view` 比 `GraphKey` 多一个 overview：它不是一张图，但共用同一份文档。 */
type View = "information" | "attack" | "overview";

const EMPTY_FLOW: BuiltFlow = {
  nodes: [],
  edges: [],
  focusId: null,
  inbound: 0,
  outbound: 0,
  loops: 0,
  hidden: { inbound: [], outbound: [] },
  dangling: 0,
};

export function SessionGraph({ sessionId = null }: { sessionId?: string | null }) {
  const [view, setView] = useState<View>("information");
  const [nav, setNav] = useState<Nav>({ ids: [], at: -1 });
  const [selection, setSelection] = useState<Selection>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [source, setSource] = useState<GraphSource>(() => initialGraphSource(sessionId));
  const [reloadToken, setReloadToken] = useState(0);

  // graph 表示「当前看的是哪张图」；overview 时等于 information，但不渲染画布。
  const graph: GraphKey = view === "attack" ? "attack" : "information";

  // 数据来源与降级规则集中在 graph/graphSource.ts，文件头写清了后端现状。
  useEffect(() => {
    const controller = new AbortController();
    // 有会话才需要「读取中」；没有会话时 initialGraphSource 已经是演示数据，不必闪一下。
    setSource(initialGraphSource(sessionId));
    loadSessionGraph(sessionId, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setSource(next);
      })
      .catch(() => {
        // 只有 abort 会抛到这里，保留当前状态即可。
      });
    return () => controller.abort();
  }, [sessionId, reloadToken]);

  useEffect(() => {
    setNav({ ids: [], at: -1 });
    setSelection(null);
    setPanel(null);
  }, [sessionId]);

  const document = source.status === "remote" || source.status === "demo" ? source.document : null;

  // 没有历史时交给布局层回退到文档里的第一个对象。
  const activeId = nav.ids[nav.at] ?? null;

  const built = useMemo(() => {
    if (!document) return EMPTY_FLOW;
    return graph === "information"
      ? buildInformationFlow(document, activeId, selection)
      : buildAttackFlow(document, activeId, selection);
  }, [document, graph, activeId, selection]);

  // 历史为空时，把布局层回退出来的默认焦点补成第一条。
  // 否则第一次点选之后，「后退」只能退回一步，回不到最开始的起点。
  useEffect(() => {
    if (nav.ids.length > 0 || !built.focusId) return;
    setNav({ ids: [built.focusId], at: 0 });
  }, [built.focusId, nav.ids.length]);

  const resolved = useMemo(
    () => (document ? resolve(document, graph, built.focusId, selection) : null),
    [document, graph, built.focusId, selection],
  );

  const pickerItems = useMemo(() => {
    if (!document) return [];
    if (graph === "information") {
      return document.information_graph.nodes.map((item) => ({
        id: item.id,
        name: item.label,
        meta: `${
          document.information_graph.edges.filter(
            (edge) => edge.from_id === item.id || edge.to_id === item.id,
          ).length
        } 条关系`,
      }));
    }
    return document.attack_graph.nodes.map((item) => ({
      id: item.id,
      name: item.summary,
      meta: `${
        document.attack_graph.edges.filter(
          (edge) => edge.from_id === item.id || edge.to_id === item.id,
        ).length
      } 次尝试`,
    }));
  }, [document, graph]);

  const totals = !document
    ? { nodes: 0, edges: 0 }
    : graph === "information"
      ? {
          nodes: document.information_graph.nodes.length,
          edges: document.information_graph.edges.length,
        }
      : { nodes: document.attack_graph.nodes.length, edges: document.attack_graph.edges.length };

  const goFocus = (id: string) => {
    setSelection(null);
    setPanel(null);
    setNav((prev) => {
      if (prev.ids[prev.at] === id) return prev;
      const kept = prev.ids.slice(0, prev.at + 1);
      const next = [...kept, id].slice(-HISTORY_LIMIT);
      return { ids: next, at: next.length - 1 };
    });
  };

  const canBack = nav.at > 0;
  const canForward = nav.at >= 0 && nav.at < nav.ids.length - 1;

  const back = () => {
    setSelection(null);
    setPanel(null);
    setNav((prev) => (prev.at > 0 ? { ...prev, at: prev.at - 1 } : prev));
  };

  const forward = () => {
    setSelection(null);
    setPanel(null);
    setNav((prev) => (prev.at < prev.ids.length - 1 ? { ...prev, at: prev.at + 1 } : prev));
  };

  // Alt + ←/→ 走历史；用函数式更新，所以监听只挂一次。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!event.altKey) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        setSelection(null);
        setPanel(null);
        setNav((prev) => (prev.at > 0 ? { ...prev, at: prev.at - 1 } : prev));
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        setSelection(null);
        setPanel(null);
        setNav((prev) =>
          prev.at < prev.ids.length - 1 ? { ...prev, at: prev.at + 1 } : prev,
        );
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 降级原因只在 demo 分支有意义；先收窄类型，避免在 JSX 里做类型体操。
  const demoReason = source.status === "demo" ? source.reason : "";

  return (
    <section className="view graph-view" aria-label="双图可视化">
      <div className="graph-head">
        <div className="seg" role="tablist" aria-label="图选择">
          {(["information", "attack", "overview"] as const).map((key) => (
            <button
              key={key}
              role="tab"
              disabled={key === "attack" && source.status === "remote" && source.attackDisabled}
              aria-selected={view === key}
              className={view === key ? "active" : ""}
              onClick={() => {
                setView(key);
                setNav({ ids: [], at: -1 });
                setSelection(null);
                setPanel(null);
              }}
            >
              {key === "overview" ? OVERVIEW_TITLE : GRAPH_TITLES[key]}
            </button>
          ))}
        </div>
        <p className="graph-caption">
          {view === "overview" ? OVERVIEW_CAPTION : GRAPH_CAPTIONS[graph]}
        </p>
        <div className="graph-meta">
          <button type="button" className="mini-btn" disabled={!sessionId || source.status === "loading"}
            onClick={() => setReloadToken((n) => n + 1)}>
            {source.status === "loading" ? "读取中…" : "刷新图谱"}
          </button>
          {source.status === "demo" && (
            <span className="gdemo" title={demoReason}>
              演示数据
            </span>
          )}
          {source.status === "remote" && <span className="glive">会话数据</span>}
          {document && (
            <span className="gmeta-item">
              {FIELD_LABELS.revision} v{document.revision}
            </span>
          )}
          {document && view === "overview" && (
            <span className="gmeta-item">
              实体 {document.information_graph.nodes.length} · 关系{" "}
              {document.information_graph.edges.length} · 状态{" "}
              {document.attack_graph.nodes.length} · 尝试 {document.attack_graph.edges.length}
            </span>
          )}
          {document && view !== "overview" && (
            <span className="gmeta-item">
              共 {totals.nodes} 个{OBJECT_KIND_LABELS[graph].node} · {totals.edges} 条
              {OBJECT_KIND_LABELS[graph].edge}
            </span>
          )}
        </div>
      </div>

      {document && (
        <p className="graph-banner" role="status">
          {source.status === "remote" ? (
            <>
              <strong>来自会话图接口。</strong>
              数据读自 <code>GET /api/sessions/{"{id}"}/graph</code>；图内容属于模型维护的工作资料
              （回执中的 <code>semantic_verification: model_judgment</code>），不是独立验证结果，
              也不代表目标存在漏洞。
            </>
          ) : (
            <>
              <strong>当前显示演示数据。</strong>
              {demoReason}；数据为虚构样例，用于确认布局与交互。图内容属于模型维护的工作资料
              （回执中的 <code>semantic_verification: model_judgment</code>），
              不是独立验证结果，也不代表目标存在漏洞。
            </>
          )}
        </p>
      )}

      {document === null ? (
        <SourceState source={source} onRetry={() => setReloadToken((n) => n + 1)} />
      ) : view === "overview" ? (
        <GraphOverview
          document={document}
          onJump={(next, id) => {
            setView(next);
            setNav({ ids: [id], at: 0 });
            setSelection(null);
            setPanel(null);
          }}
        />
      ) : (
        <>
          <div className="gfocus-bar">
            <div className="gnav" role="group" aria-label="浏览历史">
              <button
                type="button"
                onClick={back}
                disabled={!canBack}
                aria-label="后退"
                title={canBack ? "后退（Alt+←）" : "没有更早的焦点"}
              >
                ←
              </button>
              <button
                type="button"
                onClick={forward}
                disabled={!canForward}
                aria-label="前进"
                title={canForward ? "前进（Alt+→）" : "没有更新的焦点"}
              >
                →
              </button>
            </div>
            <span className="gfocus-label">当前焦点</span>
            <FocusPicker
              label={OBJECT_KIND_LABELS[graph].node}
              items={pickerItems}
              value={built.focusId}
              onChange={goFocus}
            />
            <span className="gfocus-count">
              入 {built.inbound} · 出 {built.outbound}
              {built.loops > 0 ? ` · 自环 ${built.loops}` : ""}
            </span>
          </div>

          <div className="graph-body">
            <ReactFlowProvider>
              <Canvas
                document={document}
                graph={graph}
                focusId={built.focusId}
                selection={selection}
                onFocus={goFocus}
                onSelectEdge={(id) => {
                  setSelection(id ? { kind: "edge", graph, id } : null);
                  setPanel(null);
                }}
                onOpenFold={(side) => setPanel({ kind: "fold", side })}
              />
            </ReactFlowProvider>
            <aside className="gside" aria-label="节点详情">
              <SidePanel
                document={document}
                graph={graph}
                panel={panel}
                selection={selection}
                resolved={resolved}
                hidden={built.hidden}
                counts={{ inbound: built.inbound, outbound: built.outbound }}
                onFocus={goFocus}
                onSelectEdge={(id) => setSelection(id ? { kind: "edge", graph, id } : null)}
                onOpenEntity={(id) => {
                  // 跨图跳转的两个 ID 属于不同图，历史不能混着走，直接重置。
                  setView("information");
                  setNav({ ids: [id], at: 0 });
                  setSelection(null);
                  setPanel(null);
                }}
                onClosePanel={() => setPanel(null)}
              />
            </aside>
          </div>
        </>
      )}
    </section>
  );
}
