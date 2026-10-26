"""仅当前 Session 的双图入口，不执行外部操作。"""

import json
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolCallId, tool
from pydantic import Field

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.graph_sync import graph_context, present_page
from agent_workbench.domain.session_graph import read_graph
from agent_workbench.domain.session_graph import update_graph as commit_graph


@tool
async def get_graph(runtime: ToolRuntime[RunContext], include_pending: bool = True) -> str:
    """读取当前Session完整双图及版本，默认同时返回最新连续待审页 pending_page。

    图revision变化、page_id失效或没有可用页时，再调用本工具取得有效page_id。
    核对该页后调用update_graph(expected_revision=revision,page_id=pending_page.page_id,
    operations=修改列表或[])；读取本身不推进游标。include_pending=false只读图。
    内容是模型维护的不可信工作资料，不是授权或独立验证。
    """
    box = runtime.context.toolbox
    page = None
    if include_pending:
        page = await graph_context(box, None, force_page=True)
    if page and "graph" in page:
        graph = page.pop("graph")
        async with box.db.transaction() as db:
            await present_page(db, box, page["page_id"])
    else:
        async with box.db.session() as db:
            graph = await read_graph(db, box.session_id)
    if page is not None:
        graph["pending_page"] = page
    if not box.settings.graph_attack_enabled:
        graph.pop("attack_graph", None)
    return json.dumps(graph, ensure_ascii=False)


@tool
async def update_graph(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    expected_revision: int,
    operations: list[GraphOperation],
    page_id: Annotated[
        str | None,
        Field(
            description="确认已核对历史时必填当前展示的page_id；缺失仅改图。失效或无页时get_graph(include_pending=true)重新取得。禁止自行编造。"
        ),
    ] = None,
) -> str:
    """增量维护当前 Session 双图；只有携带有效 page_id 才确认本页历史。

    每项含 op、graph、data；新增用 temp_id，修改用现有 id（替换该对象的完整语义字段）。
    information 节点 data={label,source_refs}，边={from_id,to_id,relation,status,source_refs}；
    attack 节点={summary,entity_refs}，边={from_id,to_id,action,outcome,source_refs}。
    Relation只能Entity→Entity；Attempt只能State→State，Entity经State.entity_refs关联。
    简例：Entity e1/e2；Relation e1→e2；State s0(entity_refs=[e1])/s1(entity_refs=[e2])；Attempt s0→s1。
    同一State允许自环。本批temp_id可前向引用，整批唯一。
    核对同步页必须携带其page_id；无图变化用operations=[]确认。省略只改图，不确认执行历史。
    旧页失效时调用get_graph取得当前版本pending_page，不要继续提交旧page_id。
    来源用 operation:<UUID> 或 artifact:<UUID>，应真正支持所述行为和结论，可引用多个操作。
    工具执行成功不等于攻击成功；无法确认的执行不能仅因此写成 negative。
    后续独立操作成功不能证明此前unknown操作曾执行成功；不要把这种因果推断写为事实。
    不必将普通辅助操作记为攻击。本批临时 ID 可互相引用。
    保留有意义的 negative/blocked/unknown 尝试，允许自环；success 仅指该次预期结果。
    不填秘密、原始 Payload 或长正文。图不扩大 Scope，不是任务完成的前提。
    """
    box = runtime.context.toolbox
    if not box.settings.graph_attack_enabled and any(x.graph == "attack" for x in operations):
        raise ValueError("当前对照配置只启用信息图")
    async with box.db.transaction() as db:
        result = await commit_graph(
            db,
            box,
            f"{box.run_id}:{tool_call_id}",
            expected_revision,
            operations,
            None,
            page_id,
        )
        from agent_workbench.domain.graph_sync import finish_review, record_review_outcome

        await record_review_outcome(
            db, box, "accepted_update", tool_call_ids=[tool_call_id], page_id=page_id
        )
        await finish_review(db, box, "updated")
    result = {**result, "history_acknowledged": page_id is not None}
    if page_id is None:
        result["review_hint"] = (
            "仅图已更新，历史未确认；调用get_graph获取当前revision的pending_page，核对后携带新page_id提交。"
        )
    return json.dumps(result, ensure_ascii=False)


GRAPH_TOOLS = [get_graph, update_graph]
