import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type ModelSettings,
  type SessionItem,
  type ToolActivityDetail,
  type ToolActivityItem,
} from "./api/client";
import { ChatView, Composer } from "./console/ChatView";
import {
  ArtifactDialog,
  DeleteSessionDialog,
  ModelDialog,
  NewProjectDialog,
  RecoveryDialog,
  ReportDialog,
  SkillsDialog,
  ToolDialog,
  WeaknessDialog,
} from "./console/Dialogs";
import { Icon, IconSprite } from "./console/Icon";
import { Sidebar } from "./console/Sidebar";
import { TaskDrawer, type DrawerTab } from "./console/TaskDrawer";
import { Workbench } from "./console/Workbench";
import { isFixtureProject, runLabel, runTone } from "./console/labels";
import { useWorkbench } from "./console/useWorkbench";

type Modal =
  | { kind: "project" }
  | { kind: "delete"; session: SessionItem }
  | { kind: "recovery" }
  | { kind: "artifact"; id: string }
  | { kind: "report"; version: number }
  | { kind: "weakness"; id: string }
  | { kind: "tool"; detail: ToolActivityDetail; item: ToolActivityItem }
  | { kind: "model" }
  | { kind: "skills" }
  | null;

const TEMPLATE = "目标：\n范围：仅限我提供的地址\n要求：先给计划和范围，不要直接执行工具。";

export default function App() {
  const wb = useWorkbench();
  const [view, setView] = useState<"chat" | "work">("chat");
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 760);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("activity");
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [modal, setModal] = useState<Modal>(null);
  const [toast, setToast] = useState("");
  const [seed, setSeed] = useState({ text: "", nonce: 0 });
  const [reportBusy, setReportBusy] = useState(false);
  const toastTimer = useRef<number | undefined>(undefined);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2800);
  }, []);

  const openDrawer = useCallback((tab: DrawerTab) => {
    setDrawerTab(tab);
    setDrawerOpen(true);
  }, []);

  const closeModal = useCallback(() => {
    if (modal?.kind === "recovery") wb.clearAck();
    setModal(null);
  }, [modal, wb]);

  useEffect(() => {
    if (wb.ackRun) setModal({ kind: "recovery" });
  }, [wb.ackRun]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
      if (event.key === "Escape") {
        setSearchOpen(false);
        setDrawerOpen(false);
        setModal(null);
        wb.clearAck();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const over = (event: DragEvent) => event.preventDefault();
    const drop = (event: DragEvent) => {
      event.preventDefault();
      showToast("后端尚未提供附件上传接口，未保存任何文件。");
    };
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  }, [showToast]);

  const currentProject =
    wb.projects.find((project) => project.id === wb.session?.project_id) ??
    wb.projects.find((project) => project.id === wb.selectedProjectId);
  const projectName = currentProject?.name ?? "未归属项目";
  const pendingProject = wb.projects.find((project) => project.id === wb.pendingProjectId);
  const recentSession = wb.recent.find((session) => {
    const project = wb.projects.find((item) => item.id === session.project_id);
    return project ? !isFixtureProject(project.name) : true;
  });

  const handleSend = async (text: string, attachments?: string[], attachmentSession?: string, messageKey?: string) => {
    if (!wb.modelLocked) {
      if (!wb.modelSettings) {
        showToast("正在读取模型配置，请稍候再试。");
        return false;
      }
      const selected = wb.modelOptions.find((model) => model.key === wb.currentModelKey);
      if (!selected) {
        setModal({ kind: "model" });
        showToast("请先选择并配置一个模型。");
        return false;
      }
      if (!selected.configured) {
        setModal({ kind: "model" });
        showToast(`所选模型 ${selected.model} 尚未配置 Key。`);
        return false;
      }
    }
    const ok = await wb.send(text, attachments, attachmentSession, messageKey);
    if (ok) {
      setView("chat");
      setSeed({ text: "", nonce: 0 });
    }
    return ok;
  };

  const createReport = async () => {
    if (!wb.sessionId || reportBusy) return;
    setReportBusy(true);
    try {
      const report = await api.createReport(wb.sessionId);
      showToast(`报告 v${report.version} 已生成`);
      openDrawer("reports");
    } catch (cause) {
      showToast(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setReportBusy(false);
    }
  };

  const searchResults = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const pool = wb.projects.flatMap((project) =>
      project.sessions.map((session) => ({ session, project: project.name })),
    );
    const filtered = needle
      ? pool.filter(
          (item) =>
            item.session.title.toLowerCase().includes(needle) ||
            item.project.toLowerCase().includes(needle),
        )
      : pool.sort((left, right) => Date.parse(right.session.created_at) - Date.parse(left.session.created_at));
    return filtered.slice(0, 24);
  }, [query, wb.projects]);

  const matchingProjects = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    return wb.projects.filter((project) => project.name.toLowerCase().includes(needle)).slice(0, 6);
  }, [query, wb.projects]);

  const notice = wb.bootError
    ? { tone: "error", text: wb.bootError, close: false, retry: true }
    : wb.connection === "offline"
      ? {
          tone: "error",
          text: "无法连接本机服务。页面保留已加载内容；输入不会自动重发，恢复后可手动继续。",
          close: false,
          retry: true,
        }
      : wb.notice
        ? { tone: "", text: wb.notice, close: true, retry: false }
        : !wb.ready
          ? { tone: "", text: "正在连接本机服务…", close: false, retry: false }
          : null;

  return (
    <div className="rk">
      <IconSprite />
      <div className={`app${sidebarOpen ? "" : " rail"}`}>
        <Sidebar
          projects={wb.projects}
          recent={wb.recent}
          sessionId={wb.sessionId}
          skillCount={wb.skillCount}
          executorState={wb.executorState}
          executorId={wb.executorId}
          onSelectSession={(id) => {
            wb.selectSession(id);
            setView("chat");
          }}
          onNewChat={(projectId) => {
            wb.newChat(projectId);
            setView("chat");
          }}
          onNewProject={() => setModal({ kind: "project" })}
          onSearch={() => setSearchOpen(true)}
          onCollapse={() => setSidebarOpen(false)}
          onOpenSkills={() => setModal({ kind: "skills" })}
          onOpenSettings={() => setModal({ kind: "model" })}
          onDeleteSession={(session) => setModal({ kind: "delete", session })}
          onRefreshProject={(projectId) => {
            void wb
              .refreshProject(projectId)
              .then(() => showToast("会话列表已刷新"))
              .catch((cause) => showToast(String(cause)));
          }}
          isProjectLoaded={wb.isProjectLoaded}
          onLoadProject={(projectId) => void wb.ensureProjectSessions(projectId)}
        />

        <main className="main">
          <header className="topbar">
            <div className="topbar-left">
              {!sidebarOpen && (
                <button className="icon-btn" title="展开侧栏" onClick={() => setSidebarOpen(true)}>
                  <Icon name="panel" />
                </button>
              )}
              {wb.session && <span className="top-title">{wb.session.title}</span>}
            </div>
            <div className="seg" role="tablist" aria-label="视图">
              <button className={view === "chat" ? "active" : ""} onClick={() => setView("chat")}>对话</button>
              <button className={view === "work" ? "active" : ""} onClick={() => setView("work")}>运行台</button>
            </div>
            <div className="topbar-right">
              {wb.run && (
                <span className={`status-pill ${runTone(wb.run.status)}`}>{runLabel(wb.run.status)}</span>
              )}
              {wb.sessionId && (
                <button className="ghost" onClick={() => openDrawer("activity")}>任务详情</button>
              )}
              {wb.sessionId && (
                <button className="icon-btn" title="任务设置" onClick={() => openDrawer("settings")}>
                  <Icon name="more" />
                </button>
              )}
            </div>
          </header>

          {notice && (
            <div className={`notice-bar ${notice.tone}`} role="status">
              <Icon name="alert" size="sm" />
              <span>{notice.text}</span>
              {notice.retry && (
                <button
                  className="mini-btn"
                  style={{ marginLeft: "auto" }}
                  onClick={() => {
                    void wb
                      .reconnect()
                      .then(() => showToast("连接已恢复"))
                      .catch((cause) => showToast(String(cause)));
                  }}
                >
                  重试
                </button>
              )}
              {notice.close && (
                <button className="close" title="关闭" onClick={() => wb.setNotice("")}>×</button>
              )}
            </div>
          )}

          <div className="stage">
            {!wb.sessionId && view === "chat" ? (
              <section className="view empty-view">
                <div className="empty-wrap">
                  {pendingProject && (
                    <div className="empty-project">
                      <Icon name="folder" size="sm" />
                      <span>{pendingProject.name}</span>
                    </div>
                  )}
                  <h2 className="empty-title">今天想聊点什么？</h2>
                  <p className="empty-sub">描述目标或指令，我先给计划，再按你授权的范围执行。</p>
                  <div className="composer-wrap">
                    <Composer
                      attachmentsEnabled={wb.attachmentsEnabled}
                      sessionId={wb.sessionId}
                      onPrepareAttachments={wb.prepareAttachmentSession}
                      placeholder="和 Rexak 一起工作"
                      disabled={!wb.ready}
                      busy={wb.busy}
                      scopeMode={null}
                      models={wb.modelOptions}
                      modelKey={wb.currentModelKey}
                      modelLocked={wb.modelLocked}
                      onModelChange={wb.setSelectedModelKey}
                      onConfigureModel={() => setModal({ kind: "model" })}
                      onScope={() => showToast("新任务默认按指令运行；目标从首条指令解析，创建后可在任务详情查看范围。")}
                      onSkills={() => setModal({ kind: "skills" })}
                      onSend={handleSend}
                      onStop={() => undefined}
                      stopper={false}
                      onToast={showToast}
                      seed={seed}
                    />
                  </div>
                  <div className="starters">
                    {recentSession && (
                      <button
                        className="starter"
                        onClick={() => {
                          wb.selectSession(recentSession.id);
                          setView("chat");
                        }}
                      >
                        <Icon name="link" />
                        打开「{recentSession.title}」
                        {recentSession.latest_run_status && (
                          <span className="tag">{runLabel(recentSession.latest_run_status)}</span>
                        )}
                      </button>
                    )}
                    <button className="starter" onClick={() => setSeed({ text: TEMPLATE, nonce: Date.now() })}>
                      <Icon name="shield" />
                      描述目标与约束，先要一版计划
                    </button>
                    {recentSession && (
                      <button
                        className="starter"
                        onClick={() => {
                          wb.selectSession(recentSession.id);
                          setView("work");
                        }}
                      >
                        <Icon name="activity" />
                        查看最近任务的运行台
                      </button>
                    )}
                  </div>
                </div>
              </section>
            ) : view === "work" ? (
              <Workbench
                projectName={projectName}
                snapshot={wb.snapshot}
                run={wb.run}
                plan={wb.plan}
                tools={wb.tools}
                weaknesses={wb.weaknesses}
                leads={wb.leads}
                usage={wb.usage}
                models={wb.modelSettings}
                inputRequired={wb.inputRequired}
                onControl={(action) => void wb.control(action)}
                onOpenDrawer={(tab) => openDrawer(tab as DrawerTab)}
                onOpenWeakness={(id) => setModal({ kind: "weakness", id })}
              />
            ) : wb.sessionId ? (
              <ChatView
                attachmentsEnabled={wb.attachmentsEnabled}
                sessionId={wb.sessionId}
                scopeMode={wb.session?.scope?.mode ?? null}
                run={wb.run}
                conversation={wb.conversation}
                tools={wb.tools}
                assistantText={wb.assistantText}
                assistantStartedAt={wb.assistantStartedAt}
                modelPhase={wb.modelPhase}
                inputRequired={wb.inputRequired}
                weaknesses={wb.weaknesses}
                usage={wb.usage}
                hasMoreMessages={wb.hasMoreMessages}
                hasMoreTools={wb.hasMoreTools}
                loadingMore={wb.loadingMore}
                busy={wb.busy}
                models={wb.modelOptions}
                modelKey={wb.currentModelKey}
                modelLocked={wb.modelLocked}
                onModelChange={wb.setSelectedModelKey}
                onConfigureModel={() => setModal({ kind: "model" })}
                onSend={handleSend}
                onPrepareAttachments={wb.prepareAttachmentSession}
                onControl={(action) => void wb.control(action)}
                onAck={() => setModal({ kind: "recovery" })}
                onOpenDrawer={(tab) => openDrawer(tab as DrawerTab)}
                onOpenWeakness={(id) => setModal({ kind: "weakness", id })}
                onOpenEvidence={(id) => setModal({ kind: "artifact", id })}
                onOpenTool={(detail, item) => setModal({ kind: "tool", detail, item })}
                onLoadOlderMessages={() => void wb.loadOlderMessages()}
                onLoadMoreTools={() => void wb.loadMoreTools()}
                onToast={showToast}
                onSkills={() => setModal({ kind: "skills" })}
                onCreateReport={() => void createReport()}
                onCreateReportBusy={reportBusy}
              />
            ) : null}
          </div>
        </main>
      </div>

      {!wb.sessionId && view === "chat" && (
        <button
          className="upload-hint"
          title="附件上传暂不可用：后端尚未提供上传接口"
          onClick={() => showToast("后端尚未提供附件上传接口，暂不可用。")}
        >
          <span className="orb"><Icon name="paperclip" size="sm" /></span>
          附件上传暂不可用
        </button>
      )}

      {searchOpen && (
        <div
          className="search-wrap on"
          onClick={(event) => event.target === event.currentTarget && setSearchOpen(false)}
        >
          <div className="search">
            <div className="search-in">
              <Icon name="search" style={{ color: "#9b9ba3" }} />
              <input
                autoFocus
                value={query}
                placeholder="搜索任务、项目…"
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && searchResults[0]) {
                    wb.selectSession(searchResults[0].session.id);
                    setSearchOpen(false);
                    setView("chat");
                  }
                }}
              />
              <span className="esc">ESC</span>
            </div>
            <div className="search-list">
              {searchResults.map((item) => (
                <button
                  className="search-item"
                  key={item.session.id}
                  onClick={() => {
                    wb.selectSession(item.session.id);
                    setSearchOpen(false);
                    setView("chat");
                  }}
                >
                  <Icon name="file" size="sm" />
                  <span>{item.session.title}</span>
                  <small>{item.project}</small>
                </button>
              ))}
              {matchingProjects.map((project) => (
                <button
                  className="search-item"
                  key={`p-${project.id}`}
                  onClick={() => {
                    wb.newChat(project.id);
                    setSearchOpen(false);
                    setView("chat");
                  }}
                >
                  <Icon name="folder" size="sm" />
                  <span>在「{project.name}」下新建对话</span>
                  <small>项目</small>
                </button>
              ))}
              {!searchResults.length && !matchingProjects.length && (
                <div className="search-empty">没有匹配的任务或项目。</div>
              )}
            </div>
          </div>
        </div>
      )}

      {drawerOpen && <div className="scrim on" onClick={() => setDrawerOpen(false)} />}
      <TaskDrawer
        open={drawerOpen}
        tab={drawerTab}
        onTab={setDrawerTab}
        onClose={() => setDrawerOpen(false)}
        sessionId={wb.sessionId}
        projectId={currentProject?.id ?? wb.defaultProjectId}
        sessionTitle={wb.session?.title ?? ""}
        snapshot={wb.snapshot}
        modelSettings={wb.modelSettings}
        onModelsSaved={(settings: ModelSettings) => {
          wb.onModelsSaved(settings);
          showToast("模型配置已保存");
        }}
        onOpenReport={(version) => setModal({ kind: "report", version })}
        refreshToken={(wb.snapshot?.event_cursor ?? 0)}
        onToast={showToast}
      />

      {modal?.kind === "project" && (
        <NewProjectDialog
          onClose={() => setModal(null)}
          onCreate={async (name, description) => {
            const project = await wb.createProject(name, description);
            wb.newChat(project.id);
            showToast(`项目「${project.name}」已创建`);
            return true;
          }}
        />
      )}
      {modal?.kind === "delete" && (
        <DeleteSessionDialog
          target={modal.session}
          onClose={() => setModal(null)}
          onConfirm={async (terminate) => {
            const result = await wb.deleteSession(modal.session, terminate);
            if (result.ok) {
              showToast(result.pending ? "任务已移除；取消仍在确认中。" : "任务已删除。");
            }
            return result;
          }}
        />
      )}
      {modal?.kind === "recovery" && (wb.ackRun ?? wb.run) && (
        <RecoveryDialog
          run={(wb.ackRun ?? wb.run)!}
          onClose={closeModal}
          onConfirm={wb.resumeWithAcknowledgement}
        />
      )}
      {modal?.kind === "artifact" && (
        <ArtifactDialog artifactId={modal.id} onClose={() => setModal(null)} />
      )}
      {modal?.kind === "report" && wb.sessionId && (
        <ReportDialog sessionId={wb.sessionId} version={modal.version} onClose={() => setModal(null)} />
      )}
      {modal?.kind === "weakness" && wb.sessionId && (
        <WeaknessDialog
          sessionId={wb.sessionId}
          weaknessId={modal.id}
          onClose={() => setModal(null)}
          onOpenArtifact={(id) => setModal({ kind: "artifact", id })}
        />
      )}
      {modal?.kind === "tool" && (
        <ToolDialog detail={modal.detail} item={modal.item} onClose={() => setModal(null)} />
      )}
      {modal?.kind === "model" && (
        <ModelDialog
          settings={wb.modelSettings}
          onSaved={(settings) => {
            wb.onModelsSaved(settings);
            showToast("模型配置已保存");
          }}
          onClose={() => setModal(null)}
        />
      )}
      {modal?.kind === "skills" && (
        <SkillsDialog
          sessionId={wb.sessionId}
          projectId={currentProject?.id ?? wb.defaultProjectId}
          onClose={() => setModal(null)}
          onToast={showToast}
        />
      )}

      <div className={`toast${toast ? " on" : ""}`}>{toast}</div>
    </div>
  );
}
