import { useState } from "react";
import type { SessionItem } from "../api/client";
import { Icon } from "./Icon";
import { isFixtureProject, shortId } from "./labels";
import type { ExecutorState, ProjectGroup } from "./useWorkbench";

function SessionDot({ status }: { status?: string | null }) {
  if (!status) return null;
  if (["running", "waiting_tool"].includes(status)) return <span className="dot run" />;
  if (status === "waiting_user") return <span className="dot ask" />;
  if (["queued", "pausing", "paused", "cancelling", "recovering"].includes(status)) {
    return <span className="dot pause" />;
  }
  return null;
}

export function Sidebar({
  projects,
  recent,
  sessionId,
  skillCount,
  executorState,
  executorId,
  onSelectSession,
  onNewChat,
  onNewProject,
  onSearch,
  onCollapse,
  onOpenSkills,
  onOpenSettings,
  onDeleteSession,
  onRefreshProject,
  isProjectLoaded,
  onLoadProject,
}: {
  projects: ProjectGroup[];
  recent: SessionItem[];
  sessionId: string;
  skillCount: number | null;
  executorState: ExecutorState;
  executorId: string | null;
  onSelectSession: (id: string) => void;
  onNewChat: (projectId?: string | null) => void;
  onNewProject: () => void;
  onSearch: () => void;
  onCollapse: () => void;
  onOpenSkills: () => void;
  onOpenSettings: () => void;
  onDeleteSession: (session: SessionItem) => void;
  onRefreshProject: (projectId: string) => void;
  isProjectLoaded: (projectId: string) => boolean;
  onLoadProject: (projectId: string) => void;
}) {
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [showFixtures, setShowFixtures] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const visibleProjects = projects.filter((project) => showFixtures || !isFixtureProject(project.name));
  const hiddenCount = projects.length - visibleProjects.length;
  const projectNames = new Map(projects.map((project) => [project.id, project.name]));
  const visibleRecent = recent.filter(
    (session) => showFixtures || !isFixtureProject(projectNames.get(session.project_id) ?? ""),
  );

  const toggleProject = (id: string) => {
    setClosed((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const executorText =
    executorState === "online"
      ? "在线"
      : executorState === "offline"
        ? "不可达"
        : executorState === "checking"
          ? "查询中"
          : "未查询";
  const executorClass =
    executorState === "online" ? "" : executorState === "offline" ? " off" : " check";

  const renderSession = (session: SessionItem) => (
    <div className="sess-wrap" key={session.id}>
      <button
        className={`sess ${sessionId === session.id ? "active" : ""}`}
        data-sess={session.id}
        title={session.title}
        onClick={() => onSelectSession(session.id)}
      >
        <span className="sess-title">{session.title}</span>
        <SessionDot status={session.latest_run_status} />
      </button>
      <button
        className="sess-act"
        title="删除任务"
        aria-label={`删除任务 ${session.title}`}
        onClick={() => onDeleteSession(session)}
      >
        <Icon name="close" size="sm" />
      </button>
    </div>
  );

  return (
    <aside className="sidebar" aria-label="项目与任务">
      <div className="side-head">
        <div className="brand" title="Rexak Agent">
          <span className="brand-mark">R</span>
          <span className="brand-name">Rexak Agent</span>
        </div>
        <div className="side-tools">
          <button className="icon-btn" title="收起侧栏" onClick={onCollapse}>
            <Icon name="panel" />
          </button>
          <button className="icon-btn" title="新对话" onClick={() => onNewChat()}>
            <Icon name="edit" />
          </button>
          <button className="icon-btn" title="搜索任务（Ctrl/Cmd+K）" onClick={onSearch}>
            <Icon name="search" />
          </button>
        </div>
      </div>

      <button className="new-chat" onClick={() => onNewChat()}>
        <Icon name="edit" />
        新对话
        <span className="plus">＋</span>
      </button>

      <nav className="side-scroll">
        <div className="side-label">
          项目
          <button title="新建项目" onClick={onNewProject}>
            ＋
          </button>
        </div>
        {projects.length === 0 && <p className="side-empty">还没有项目。新建项目后即可开始任务。</p>}
        {visibleProjects.map((project) => {
          const all = project.sessions;
          const isExpanded = !!expanded[project.id];
          let shown = isExpanded ? all : all.slice(0, 5);
          const selected = all.find((session) => session.id === sessionId);
          if (!isExpanded && selected && !shown.some((session) => session.id === selected.id)) {
            shown = [...shown, selected];
          }
          const remaining = all.length - shown.length;
          return (
          <div
            className={`proj ${closed.has(project.id) ? "closed" : ""}`}
            key={project.id}
            onMouseLeave={() => setOpenMenu((current) => (current === project.id ? null : current))}
          >
            <div className="proj-row">
              <button
                className="proj-head"
                onClick={() => {
                  if (!isProjectLoaded(project.id)) onLoadProject(project.id);
                  toggleProject(project.id);
                }}
                title={project.name}
              >
                <Icon name="folder" />
                <span>{project.name}</span>
                {isProjectLoaded(project.id) && !!all.length && <span className="proj-count">{all.length}</span>}
              </button>
              <div className="proj-actions">
                <button
                  className="proj-act"
                  title="项目操作"
                  onClick={() => setOpenMenu((current) => (current === project.id ? null : project.id))}
                >
                  <Icon name="more" size="sm" />
                </button>
                <button
                  className="proj-act"
                  title={`在「${project.name}」下新建对话`}
                  onClick={() => onNewChat(project.id)}
                >
                  <Icon name="edit" size="sm" />
                </button>
              </div>
              {openMenu === project.id && (
                <div className="menu-pop proj-menu" onClick={(event) => event.stopPropagation()}>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setOpenMenu(null);
                      onNewChat(project.id);
                    }}
                  >
                    <Icon name="edit" size="sm" />
                    新建对话
                  </button>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setOpenMenu(null);
                      onRefreshProject(project.id);
                    }}
                  >
                    <Icon name="refresh" size="sm" />
                    刷新会话
                  </button>
                  <div className="menu-sep" />
                  <button className="menu-item" disabled title="后端暂未提供项目重命名接口">
                    <Icon name="pencil" size="sm" />
                    重命名（暂不可用）
                  </button>
                  <button className="menu-item" disabled title="后端暂未提供项目删除接口">
                    <Icon name="close" size="sm" />
                    删除（暂不可用）
                  </button>
                </div>
              )}
            </div>
            <div className="proj-body">
              {!isProjectLoaded(project.id) ? (
                <p className="side-empty">展开读取会话…</p>
              ) : all.length ? (
                <>
                  {shown.map(renderSession)}
                  {remaining > 0 && (
                    <button
                      className="side-more"
                      onClick={() => setExpanded((previous) => ({ ...previous, [project.id]: true }))}
                    >
                      还有 {remaining} 条
                    </button>
                  )}
                </>
              ) : (
                <p className="side-empty">这个项目还没有任务。</p>
              )}
            </div>
          </div>
          );
        })}
        {hiddenCount > 0 && !showFixtures && (
          <button className="side-toggle" onClick={() => setShowFixtures(true)}>
            已隐藏 {hiddenCount} 个测试项目 · 显示
          </button>
        )}
        {showFixtures && (
          <button className="side-toggle" onClick={() => setShowFixtures(false)}>
            隐藏测试项目
          </button>
        )}

        <div className="side-label" style={{ marginTop: 8 }}>
          最近
        </div>
        {visibleRecent.length ? (
          visibleRecent.slice(0, 6).map(renderSession)
        ) : (
          <p className="side-empty">还没有历史任务。</p>
        )}
      </nav>

      <div className="side-foot">
        <button className="foot-row" onClick={onOpenSkills}>
          <Icon name="spark" size="sm" />
          技能库
          {skillCount !== null && <span className="badge">{skillCount}</span>}
        </button>
        <button className="foot-row" onClick={onOpenSettings}>
          <Icon name="settings" size="sm" />
          模型设置
        </button>
        <div className="exec" title={executorState === "offline" ? "执行端不可达，真实状态以执行资源页为准" : ""}>
          <span className={`live${executorClass}`} />
          {executorId ? shortId(executorId, 14) : "执行端"}
          <span className="right">{executorText}</span>
        </div>
        <div className="profile">
          <span className="avatar">L</span>
          <span>
            <span className="profile-name">本地工作台</span>
            <small>个人模式 · 本机</small>
          </span>
          <Icon name="chev-d" size="sm" className="more" />
        </div>
      </div>
    </aside>
  );
}
