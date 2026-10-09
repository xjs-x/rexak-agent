"""证据层：不可变工件存储、索引、脱敏视图、验证记录。

规则（BUILD_PLAN §5.3）：
- 大正文**不塞** checkpoint / events / findings；模型只收到短摘要与 `artifact_id`；
- 先写临时文件 → 计算哈希 → 原子替换 → 再提交元数据；
  失败时记录"待修复孤儿工件"，**不能返回不存在的可用证据**；
- 原始工件不可覆盖；新观测生成新工件，旧证据通过状态注明过期；
- HTML/脚本证据在控制台按转义文本或隔离下载展示，不能直接在应用源域执行。
"""

from __future__ import annotations

from agent_workbench.evidence.artifacts import (
    ArtifactStore,
    ArtifactWriteResult,
    StoredArtifact,
    build_artifact_store,
)

__all__ = [
    "ArtifactStore",
    "ArtifactWriteResult",
    "StoredArtifact",
    "build_artifact_store",
]
