"""服务进程的任务边界；测试/定向演练仍可显式选择运行。"""

from sqlalchemy import select

from agent_workbench.persistence.tables import ProjectRow, RunRow, SessionRow


def owned_run_ids(owner_id: str):
    return (
        select(RunRow.id)
        .join(SessionRow, RunRow.session_id == SessionRow.id)
        .outerjoin(ProjectRow, SessionRow.project_id == ProjectRow.id)
        .where(SessionRow.owner_id == owner_id, ProjectRow.deleted_at.is_(None))
    )
