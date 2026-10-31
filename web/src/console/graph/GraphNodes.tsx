/**
 * 双图的自定义节点。
 *
 * 视觉编码只使用后端真实存在的字段：
 * - 信息图实体：左侧色条表示有没有 `source_refs`，底部显示关系数与证据数。
 *   后端没有「实体类别」字段，所以这里不按类别配色，也不显示严重等级。
 * - 攻击状态：底部显示 `entity_refs` 数量、`Attempt` 数量与结果分布。
 *   分布是从真实出边/入边的 `outcome` 聚合出来的，只是索引，不是结论。
 *
 * `role` 决定节点在三列布局里站哪一列：focus（中间）/ inbound（左）/ outbound（右）。
 * 只有焦点节点用重边框，两翼压成浅色，避免一眼看过去全是同等权重的方块。
 */

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { OBJECT_KIND_LABELS, shortId } from "./graphLabels";
import type { AttackStateNodeData, EntityNodeData, FoldData } from "./graphLayout";
import type { AttemptOutcome } from "./graphSchema";
import { ATTEMPT_OUTCOME_TONES } from "./graphLabels";

const OUTCOME_ORDER: AttemptOutcome[] = ["success", "negative", "blocked", "unknown"];

/**
 * 四个方向 x 两种角色 = 8 个锚点。
 * source 与 target 各用一套 id，这样任意方向的边都能从最近的一侧出入，
 * 而不必让 React Flow 绕节点外圈。
 */
const ANCHORS: { position: Position; id: string; type: "source" | "target" }[] = [
  { position: Position.Left, id: "l-src", type: "source" },
  { position: Position.Right, id: "r-src", type: "source" },
  { position: Position.Top, id: "t-src", type: "source" },
  { position: Position.Bottom, id: "b-src", type: "source" },
  { position: Position.Left, id: "l-tgt", type: "target" },
  { position: Position.Right, id: "r-tgt", type: "target" },
  { position: Position.Top, id: "t-tgt", type: "target" },
  { position: Position.Bottom, id: "b-tgt", type: "target" },
];

function Anchors() {
  return (
    <>
      {ANCHORS.map((anchor) => (
        <Handle
          key={anchor.id}
          id={anchor.id}
          type={anchor.type}
          position={anchor.position}
          isConnectable={false}
          className="ganchor"
        />
      ))}
    </>
  );
}

export function EntityNode({ data }: NodeProps<Node<EntityNodeData>>) {
  const { entity, relationCount, evidenceCount, role } = data;
  const missingEvidence = evidenceCount === 0;
  return (
    <div
      className={`gnode gnode-entity role-${role}${missingEvidence ? " no-evidence" : ""}`}
      title={entity.label}
    >
      <Anchors />
      <div className="gnode-top">
        <span className="gnode-kind">{OBJECT_KIND_LABELS.information.node}</span>
        {role === "focus" && <span className="gnode-focus">当前</span>}
        <span className="gnode-raw" title={entity.id}>
          {shortId(entity.id, 6)}
        </span>
      </div>
      <div className="gnode-title">{entity.label}</div>
      <div className="gnode-foot">
        <span className={`gchip ${missingEvidence ? "warn" : "ok"}`}>
          {missingEvidence ? "尚无证据来源" : `证据 ${evidenceCount}`}
        </span>
        <span className="gchip">{relationCount} 条关系</span>
      </div>
    </div>
  );
}

export function AttackStateNode({ data }: NodeProps<Node<AttackStateNodeData>>) {
  const { state, entityLabels, outcomeCounts, attemptCount, role } = data;
  return (
    <div className={`gnode gnode-state role-${role}`} title={state.summary}>
      <Anchors />
      <div className="gnode-top">
        <span className="gnode-kind">{OBJECT_KIND_LABELS.attack.node}</span>
        {role === "focus" && <span className="gnode-focus">当前</span>}
        <span className="gnode-raw" title={state.id}>
          {shortId(state.id, 6)}
        </span>
      </div>
      <div className="gnode-title">{state.summary}</div>
      <div className="gnode-foot">
        <span className="gchip">{entityLabels.length} 个关联实体</span>
        <span className="gchip">{attemptCount} 次尝试</span>
        <span className="gouts" aria-label="尝试结果分布">
          {OUTCOME_ORDER.filter((outcome) => (outcomeCounts[outcome] ?? 0) > 0).map((outcome) => (
            <i
              key={outcome}
              className={`gout ${ATTEMPT_OUTCOME_TONES[outcome]}`}
              title={`${outcome}: ${outcomeCounts[outcome]}`}
            >
              {outcomeCounts[outcome]}
            </i>
          ))}
        </span>
      </div>
    </div>
  );
}

/**
 * 折叠节点：这一侧的邻居超过上限后，用一张卡片收口。
 * 它不表示任何业务对象，只表示「还有 N 个没画出来」，点击后在详情面板里列全。
 */
export function FoldNode({ data }: NodeProps<Node<FoldData>>) {
  const { side, hidden, total } = data;
  return (
    <div className={`gnode gnode-fold ${side}`}>
      <div className="gnode-fold-count">还有 {hidden} 个</div>
      <div className="gnode-fold-hint">
        {side === "inbound" ? "来源" : "去向"}未展开 · 共 {total}
      </div>
    </div>
  );
}

export const GRAPH_NODE_TYPES = {
  entity: EntityNode,
  attackState: AttackStateNode,
  fold: FoldNode,
};
