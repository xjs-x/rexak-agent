"""新用户输入接管被打断的线程；保留官方历史，停止旧节点自动重放。"""

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END


async def finish_interrupted_checkpoint(graph, config):  # type: ignore[no-untyped-def]
    # 官方清除待执行节点接口会保存已完成节点的 pending writes，不删除历史。
    await graph.aupdate_state(config, None, as_node=END)
    snapshot = await graph.aget_state(config)
    if snapshot.next:
        raise RuntimeError("旧图节点尚未清理，不能切换到新的用户输入")
    return snapshot.values, unresolved_tool_messages(snapshot.values)


def unresolved_tool_messages(values):  # type: ignore[no-untyped-def]
    pending = {}
    for message in values.get("messages", []):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                pending[call["id"]] = call
        elif isinstance(message, ToolMessage):
            pending.pop(message.tool_call_id, None)
    return [
        ToolMessage(
            id=f"interrupted-{call_id}",
            tool_call_id=call_id,
            name=call["name"],
            status="error",
            content=(
                "用户已打断上一轮操作，本工具调用不会在新指令下自动重放。"
                "若此前已提交或执行，请查询现有工具操作及任务台账确认真实状态；"
                "这条协议消息不表示远端取消成功，也不是测试结论。"
            ),
        )
        for call_id, call in pending.items()
    ]
