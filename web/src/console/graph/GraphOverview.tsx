/**
 * 全貌统计视图。
 *
 * 回答的是「这张图整体长什么样」，而不是「某个对象周围有什么」。
 * 所有数字都来自 `graphStats.summarize` 对文档的计数，
 * 没有任何评分、风险等级或模型推断。
 *
 * 「需要留意」逐条对应一个可核对的事实（缺字段、度数为零、结果未定），
 * 点开可以列出具体对象并跳过去。它们不等于「有问题」，更不等于「有漏洞」。
 */

import { useMemo } from "react";
import {
  ATTEMPT_OUTCOME_LABELS,
  ATTEMPT_OUTCOME_TONES,
  RELATION_STATUS_LABELS,
  RELATION_STATUS_TONES,
} from "./graphLabels";
import { maxOf, summarize, ATTEMPT_OUTCOME_VALUES, RELATION_STATUS_VALUES } from "./graphStats";
import type { GraphKey, SessionGraphDocument } from "./graphSchema";

function Bar({ value, max, tone }: { value: number; max: number; tone: string }) {
  const width = max > 0 ? Math.max(value > 0 ? 2 : 0, Math.round((value / max) * 100)) : 0;
  return (
    <span className="gbar" aria-hidden="true">
      <i className={`gbar-fill ${tone}`} style={{ width: `${width}%` }} />
    </span>
  );
}

function StatRow({
  label,
  value,
  max,
  tone,
}: {
  label: string;
  value: number;
  max: number;
  tone: string;
}) {
  return (
    <div className="gstat-row">
      <span className="gstat-label">{label}</span>
      <Bar value={value} max={max} tone={tone} />
      <span className="gstat-num">{value}</span>
    </div>
  );
}

/** 一条「需要留意」：可展开，展开后列出具体对象，点了就跳过去。 */
function Issue({
  title,
  hint,
  items,
  kind,
  onJump,
}: {
  title: string;
  hint: string;
  items: { id: string; name: string }[];
  kind: GraphKey;
  onJump: (graph: GraphKey, id: string) => void;
}) {
  if (!items.length) return null;
  return (
    <details className="gissue">
      <summary>
        <span className="gissue-title">{title}</span>
        <span className="gissue-count">{items.length}</span>
      </summary>
      <p className="gissue-hint">{hint}</p>
      <ul className="gissue-list">
        {items.map((item) => (
          <li key={item.id}>
            <button type="button" className="glink" onClick={() => onJump(kind, item.id)}>
              {item.name}
            </button>
          </li>
        ))}
      </ul>
    </details>
  );
}

export function GraphOverview({
  document,
  onJump,
}: {
  document: SessionGraphDocument;
  onJump: (graph: GraphKey, id: string) => void;
}) {
  const stats = useMemo(() => summarize(document), [document]);

  const statusMax = maxOf(RELATION_STATUS_VALUES.map((key) => stats.relationStatus[key]));
  const outcomeMax = maxOf(ATTEMPT_OUTCOME_VALUES.map((key) => stats.outcome[key]));

  const issues = [
    {
      title: "实体没有任何证据来源",
      hint: "这些实体只有模型登记的标签，没有可追溯的 operation 或 artifact。不能据此认定事实。",
      items: stats.entitiesWithoutEvidence.map((node) => ({ id: node.id, name: node.label })),
      kind: "information" as const,
    },
    {
      title: "关系是假设且没有来源",
      hint: "状态为 hypothesis 但 source_refs 为空，属于纯推断，使用时需要先补证据。",
      items: stats.unsourcedHypotheses.map((edge) => ({
        id: edge.from_id,
        name: edge.relation,
      })),
      kind: "information" as const,
    },
    {
      title: "实体没有任何关系",
      hint: "既没有入边也没有出边。可能是刚登记还没连上，也可能确实是孤立对象。",
      items: stats.relationlessEntities.map((node) => ({ id: node.id, name: node.label })),
      kind: "information" as const,
    },
    {
      title: "攻击状态没有关联实体",
      hint: "这些状态没有 entity_refs，无法从信息图追溯它依据的是哪个对象。",
      items: stats.statesWithoutEntities.map((state) => ({ id: state.id, name: state.summary })),
      kind: "attack" as const,
    },
    {
      title: "尝试结果无法确认",
      hint: "unknown 既不能证明发生过成功，也不能证明没发生。恢复与后续判断都不能据此推进。",
      items: stats.undecidedAttempts.map((edge) => ({ id: edge.from_id, name: edge.action })),
      kind: "attack" as const,
    },
  ].filter((issue) => issue.items.length > 0);

  return (
    <div className="goverview">
      <div className="gtotals">
        <div className="gtotal">
          <strong>{stats.entities}</strong>
          <span>实体</span>
        </div>
        <div className="gtotal">
          <strong>{stats.relations}</strong>
          <span>关联关系</span>
        </div>
        <div className="gtotal">
          <strong>{stats.states}</strong>
          <span>攻击状态</span>
        </div>
        <div className="gtotal">
          <strong>{stats.attempts}</strong>
          <span>测试尝试</span>
        </div>
        <div className="gtotal">
          <strong>{stats.loops}</strong>
          <span>其中自环</span>
        </div>
      </div>

      <div className="gpanels">
        <section className="gpanel">
          <h4>关系状态分布</h4>
          <p className="gpanel-sub">
            共 {stats.relations} 条关联关系。状态是模型对该关系的判断，不是独立验证结论。
          </p>
          {RELATION_STATUS_VALUES.map((key) => (
            <StatRow
              key={key}
              label={RELATION_STATUS_LABELS[key]}
              value={stats.relationStatus[key]}
              max={statusMax}
              tone={RELATION_STATUS_TONES[key]}
            />
          ))}
        </section>

        <section className="gpanel">
          <h4>尝试结果分布</h4>
          <p className="gpanel-sub">
            共 {stats.attempts} 次测试尝试，其中 {stats.loops} 次是同一状态上的原地重试。
          </p>
          {ATTEMPT_OUTCOME_VALUES.map((key) => (
            <StatRow
              key={key}
              label={ATTEMPT_OUTCOME_LABELS[key]}
              value={stats.outcome[key]}
              max={outcomeMax}
              tone={ATTEMPT_OUTCOME_TONES[key]}
            />
          ))}
        </section>
      </div>

      <section className="gpanel">
        <h4>需要留意</h4>
        <p className="gpanel-sub">
          下面每一条都对应一个可以直接核对的字段事实，不是风险评级。点开可以看到具体对象。
        </p>
        {issues.length ? (
          issues.map((issue) => (
            <Issue
              key={issue.title}
              title={issue.title}
              hint={issue.hint}
              items={issue.items}
              kind={issue.kind}
              onJump={onJump}
            />
          ))
        ) : (
          <p className="gempty">
            这几类检查在当前文档里都没有命中。这只说明字段层面没有缺口，不代表目标没有问题。
          </p>
        )}
      </section>
    </div>
  );
}
