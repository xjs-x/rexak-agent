import { useEffect, useState } from "react";
import type { ProjectItem, SessionItem } from "../api/client";
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
  standalone,
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
  onRenameSession,
  onRenameProject,
  onRefreshProject,
  isProjectLoaded,
  onLoadProject,
}: {
  projects: ProjectGroup[];
  standalone: SessionItem[];
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
  onRenameSession: (session: SessionItem) => void;
  onRenameProject: (project: ProjectItem) => void;
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
  const [context, setContext] = useState<{ session: SessionItem; x: number; y: number } | null>(null);
  useEffect(() => {
    if (!context && !openMenu) return;
    const dismiss = () => { setContext(null); setOpenMenu(null); };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") dismiss(); };
    window.addEventListener("pointerdown", dismiss);
    window.addEventListener("keydown", key);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", dismiss, true);
    return () => {
      window.removeEventListener("pointerdown", dismiss);
      window.removeEventListener("keydown", key);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", dismiss, true);
    };
  }, [context, openMenu]);
  const showSessionMenu = (session: SessionItem, x: number, y: number) => {
    setOpenMenu(null);
    setContext({ session, x: Math.max(8, Math.min(x, window.innerWidth - 248)),
      y: Math.max(8, Math.min(y, window.innerHeight - 112)) });
  };

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
    <div className="sess-wrap" key={session.id} onContextMenu={(event) => {
      event.preventDefault(); showSessionMenu(session, event.clientX, event.clientY);
    }}>
      <button
        className={`sess ${sessionId === session.id ? "active" : ""}`}
        data-sess={session.id}
        title={session.title}
        onClick={() => onSelectSession(session.id)}
        onKeyDown={(event) => {
          if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
            event.preventDefault(); const rect = event.currentTarget.getBoundingClientRect();
            showSessionMenu(session, rect.left + 24, rect.bottom);
          }
        }}
      >
        <span className="sess-title">{session.title}</span>
        <SessionDot status={session.latest_run_status} />
      </button>
      <button
        className="sess-act"
        title="对话操作"
        aria-label={`对话操作 ${session.title}`}
        aria-haspopup="menu"
        onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect();
          showSessionMenu(session, rect.right, rect.bottom); }}
      >
        <Icon name="more" size="sm" />
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
        <div className="side-label">聊天</div>
        {standalone.length ? standalone.map(renderSession) : <p className="side-empty">新对话会独立保存在这里。</p>}

        <div className="side-label">
          项目
          <button title="新建项目" onClick={onNewProject}>
            ＋
          </button>
        </div>
        {projects.length === 0 && <p className="side-empty">项目用于整理聊天，可以随时创建。</p>}
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
          >
            <div className="proj-row" onContextMenu={(event) => {
              event.preventDefault(); setContext(null); setOpenMenu(project.id);
            }}>
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
                  onPointerDown={(event) => event.stopPropagation()}
                  onClick={() => { setContext(null); setOpenMenu((current) => (current === project.id ? null : project.id)); }}
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
                <div className="menu-pop proj-menu" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()}>
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
                  <button className="menu-item" onClick={() => { setOpenMenu(null); onRenameProject(project); }}>
                    <Icon name="pencil" size="sm" />
                    重命名
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

      </nav>

      {context && (
        <div className="chat-context-menu" role="menu" aria-label="对话操作"
          style={{ left: context.x, top: context.y }} onPointerDown={(event) => event.stopPropagation()}>
          <button className="menu-item" role="menuitem" autoFocus onClick={() => {
            onRenameSession(context.session); setContext(null);
          }}><Icon name="pencil" />重命名</button>
          <div className="menu-sep" />
          <button className="menu-item danger" role="menuitem" onClick={() => {
            onDeleteSession(context.session); setContext(null);
          }}><Icon name="trash" />删除</button>
        </div>
      )}

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
