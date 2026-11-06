"""Double-click evaluation launcher. Opening this window never starts an assessment."""

from __future__ import annotations

import argparse
import asyncio
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_workbench.contracts.delegation import ExecutionMode  # noqa: E402
from agent_workbench.platform_compat import (  # noqa: E402
    configure_event_loop_policy,
    configure_stdio_utf8,
)
from agent_workbench.tsecbench.config import BenchmarkConfig  # noqa: E402
from agent_workbench.tsecbench.launcher import (  # noqa: E402
    ConsoleProcess,
    connect_windows_vpn,
    console_url,
    evaluate_serial,
    inspect_readiness,
    install_desktop_shortcut,
    save_profile,
    validate_inputs,
)
from agent_workbench.tsecbench.platform import ChallengeInfo  # noqa: E402


class EvaluationWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config: BenchmarkConfig | None = None
        self.rows: list[ChallengeInfo] = []
        self.current_session: str | None = None
        self.busy = False
        self.running = False
        self.closed = False
        self.stop = threading.Event()
        self.console = ConsoleProcess()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.controls: list[ttk.Widget] = []
        root.title("Rexak 评测启动器")
        root.geometry("960x820")
        root.minsize(880, 760)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Hint.TLabel", foreground="#536575", font=("Microsoft YaHei UI", 9))

        page = ttk.Frame(root, padding=22)
        page.pack(fill="both", expand=True)
        ttk.Label(page, text="Rexak · 本地评测", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            page, text="填写本轮配置 → 连接 VPN → 检查并读取题目 → 开始测评", style="Hint.TLabel"
        ).pack(anchor="w", pady=(5, 14))

        self.base = tk.StringVar(value="https://tsecbench.zc.tencent.com")
        self.token = tk.StringVar()
        self.vpn = tk.StringVar()
        self.mode = tk.StringVar(value="ultra")
        self.minutes = tk.StringVar(value="60")
        self.scope = tk.StringVar(value="单题")
        self.challenge = tk.StringVar()
        self.status = tk.StringVar(value="尚未开始测评。配置和密钥只保存在本机仓库之外。")

        fields = ttk.LabelFrame(page, text="本轮平台配置", padding=12)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        self._entry(fields, "平台地址", self.base, 0)
        self._entry(fields, "平台 Token", self.token, 1, secret=True)
        self._entry(fields, "VPN 配置", self.vpn, 2)
        choose = ttk.Button(fields, text="选择 .ovpn", command=self.choose_vpn)
        choose.grid(row=2, column=2, padx=(8, 0))
        self.controls.append(choose)
        ttk.Label(
            fields,
            text="Token 每次重新填写。VPN 已连接时可留空；更换任务前请先断开上一轮 VPN。",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))

        arrange = ttk.LabelFrame(page, text="测评安排", padding=12)
        arrange.pack(fill="x", pady=12)
        arrange.columnconfigure(3, weight=1)
        ttk.Label(arrange, text="Agent 模式").grid(row=0, column=0, sticky="w")
        combo = ttk.Combobox(
            arrange,
            textvariable=self.mode,
            values=["ultra", "standard"],
            state="readonly",
            width=13,
        )
        combo.grid(row=0, column=1, padx=8, sticky="w")
        self.controls.append(combo)
        ttk.Label(arrange, text="单题等待上限（分钟）").grid(
            row=0, column=2, sticky="w", padx=(12, 0)
        )
        limit = ttk.Entry(arrange, textvariable=self.minutes, width=10)
        limit.grid(row=0, column=3, sticky="w", padx=8)
        self.controls.append(limit)
        ttk.Label(arrange, text="运行范围").grid(row=1, column=0, sticky="w", pady=(10, 0))
        scope = ttk.Combobox(
            arrange,
            textvariable=self.scope,
            values=["单题", "全部未完成题目（串行）"],
            state="readonly",
            width=26,
        )
        scope.grid(row=1, column=1, columnspan=2, sticky="w", padx=8, pady=(10, 0))
        self.controls.append(scope)
        self.challenge_box = ttk.Combobox(arrange, textvariable=self.challenge, state="readonly")
        self.challenge_box.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.controls.append(self.challenge_box)
        ttk.Label(
            arrange,
            text="模型及模型 API Key 沿用网页中的默认模型设置。整场有效期以平台为准。",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        actions = ttk.Frame(page)
        actions.pack(fill="x")
        for text, action in [
            ("1. 连接本轮 VPN", self.connect),
            ("2. 启动控制台", self.start_console),
            ("3. 检查并读取题目", self.check),
            ("4. 开始测评", self.start),
        ]:
            button = ttk.Button(actions, text=text, command=action)
            button.pack(side="left", padx=(0, 7))
            self.controls.append(button)
        shortcuts = ttk.Frame(page)
        shortcuts.pack(fill="x", pady=(10, 0))
        ttk.Button(shortcuts, text="查看当前做题过程 / 模型设置", command=self.open_console).pack(
            side="left"
        )
        self.stop_button = ttk.Button(
            shortcuts, text="当前题结束后停止", command=self.stop_after_current, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)
        shortcut = ttk.Button(shortcuts, text="创建桌面快捷方式", command=self.desktop_shortcut)
        shortcut.pack(side="right")
        self.controls.append(shortcut)
        ttk.Label(page, textvariable=self.status, wraplength=890).pack(anchor="w", pady=(12, 6))
        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        log_frame = ttk.Frame(page)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_frame,
            height=8,
            wrap="word",
            state="disabled",
            bg="#10212d",
            fg="#e1edf4",
            font=("Microsoft YaHei UI", 9),
            relief="flat",
            padx=10,
            pady=10,
        )
        self.log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)
        ttk.Label(
            page,
            text="打开窗口、连接 VPN、启动控制台和读取题目均不会开始解题。只有“开始测评”会启动靶场和模型。",
            style="Hint.TLabel",
            wraplength=890,
        ).pack(anchor="w", pady=(8, 0))
        for value in (self.base, self.token, self.vpn, self.mode, self.minutes):
            value.trace_add("write", self.invalidate)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)

    def _entry(
        self,
        parent: tk.Misc,
        label: str,
        variable: tk.StringVar,
        row: int,
        *,
        secret: bool = False,
    ) -> None:
        ttk.Label(parent, text=label, width=12).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(parent, textvariable=variable, show="•" if secret else "")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self.controls.append(entry)

    def invalidate(self, *_args: object) -> None:
        self.config = None
        self.current_session = None
        self.rows = []
        self.challenge.set("")
        self.challenge_box.configure(values=[])

    def choose_vpn(self) -> None:
        path = filedialog.askopenfilename(
            title="选择平台下载的本轮 VPN 配置", filetypes=[("VPN 配置", "*.ovpn")]
        )
        if path:
            self.vpn.set(path)

    def configuration(self) -> BenchmarkConfig:
        if self.config is None:
            base, token, seconds = validate_inputs(
                self.base.get(), self.token.get(), self.minutes.get()
            )
            vpn = (
                Path(self.vpn.get().strip().strip('"')).expanduser()
                if self.vpn.get().strip()
                else None
            )
            self.config = save_profile(base, token, vpn, ExecutionMode(self.mode.get()), seconds)
        return self.config

    def emit(self, kind: str, value: object) -> None:
        self.events.put((kind, value))

    def launch(self, label: str, action, *, running: bool = False) -> None:
        if self.busy:
            return
        self.busy, self.running = True, running
        self.status.set(label)
        for control in self.controls:
            control.state(["disabled"])
        self.stop_button.configure(state="normal" if running else "disabled")
        self.progress.start(12)

        def work() -> None:
            try:
                action()
            except Exception as exc:
                # Surface unexpected errors at the UI boundary; never silently report success.
                self.emit("error", f"{type(exc).__name__}：{exc}")
            finally:
                self.emit("done", None)

        threading.Thread(target=work, daemon=False).start()

    def guarded(self, action) -> None:
        try:
            action(self.configuration())
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror("配置未完成", str(exc), parent=self.root)

    def connect(self) -> None:
        def action(config: BenchmarkConfig) -> None:
            self.launch(
                "已提交连接请求，请稍后检查 VPN。",
                lambda: self.emit(
                    "log",
                    "已请求 OpenVPN 连接："
                    + connect_windows_vpn(config)
                    + "。若客户端出现权限提示，请在本机完成。",
                ),
            )

        self.guarded(action)

    def start_console(self) -> None:
        def action(config: BenchmarkConfig) -> None:
            def work() -> None:
                asyncio.run(self.console.start(config))
                self.emit("log", "控制台已就绪。模型密钥可在网页设置中修改；尚未开始测评。")

            self.launch("正在启动本地控制台，不恢复旧任务……", work)

        self.guarded(action)

    def check(self) -> None:
        def action(config: BenchmarkConfig) -> None:
            def work() -> None:
                rows = asyncio.run(inspect_readiness(config))
                self.emit("rows", rows)
                self.emit(
                    "log",
                    f"检查通过：{len(rows)} 题 / {sum(row.flag_count for row in rows)} 个 flag；"
                    f"未完成 {sum(not row.is_completed for row in rows)} 题。尚未开始测评。",
                )

            self.launch("正在检查两端网络、控制台和题目列表……", work)

        self.guarded(action)

    def start(self) -> None:
        if self.busy:
            return
        if not self.rows:
            messagebox.showinfo("先检查准备情况", "请先完成“检查并读取题目”。", parent=self.root)
            return
        if self.scope.get() == "单题":
            index = self.challenge_box.current()
            pending = [row for row in self.rows if not row.is_completed]
            codes = [pending[index].unique_code] if 0 <= index < len(pending) else []
        else:
            codes = [row.unique_code for row in self.rows if not row.is_completed]
        if not codes:
            messagebox.showinfo("没有待测题目", "请选择未完成的题目。", parent=self.root)
            return
        config = self.configuration()
        detail = (
            f"平台：{config.base_url}\n范围：{len(codes)} 题，串行运行\n"
            f"Agent：{config.execution_mode.value}\n单题等待上限：{config.max_seconds / 60:g} 分钟\n\n"
            "开始后将启动靶场并调用网页中配置的模型。确认开始？"
        )
        if not messagebox.askyesno("开始本轮评测", detail, parent=self.root):
            return
        self.stop.clear()

        def work() -> None:
            # Recheck after user confirmation; stale lists must never authorize a new target.
            asyncio.run(inspect_readiness(config))
            outcomes = asyncio.run(evaluate_serial(config, codes, self.stop, self.emit))
            self.emit("log", f"本次执行已结束，共处理 {len(outcomes)} 题。成绩以平台记录为准。")

        self.launch("测评进行中，可在网页查看当前题。", work, running=True)

    def stop_after_current(self) -> None:
        self.stop.set()
        self.status.set("当前题结束后停止。若要取消当前题，请在网页点击取消，启动器会等待收尾。")
        self.stop_button.configure(state="disabled")

    def open_console(self) -> None:
        try:
            config = self.configuration()
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            messagebox.showinfo("请先填写配置", str(exc), parent=self.root)
            return
        webbrowser.open(console_url(config, self.current_session))

    def desktop_shortcut(self) -> None:
        try:
            install_desktop_shortcut()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror("快捷方式未创建", str(exc), parent=self.root)
        else:
            messagebox.showinfo("快捷方式已创建", "桌面名称：Rexak 评测启动器", parent=self.root)

    def drain(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind in {"log", "error"}:
                    text = str(value)
                    for secret in (self.token.get(), self.config.api_token if self.config else ""):
                        if secret:
                            text = text.replace(secret, "[已隐藏]")
                    self.log.configure(state="normal")
                    self.log.insert("end", text + "\n")
                    if int(self.log.index("end-1c").split(".")[0]) > 1500:
                        self.log.delete("1.0", "300.0")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                    self.status.set(text if kind == "error" else text.splitlines()[-1])
                    if self.config and self.config.env_file:
                        with (Path(self.config.env_file).parent / "launcher.log").open(
                            "a", encoding="utf-8"
                        ) as log:
                            log.write(text + "\n")
                elif kind == "rows":
                    self.rows = list(value)  # type: ignore[arg-type]
                    self.challenge_box.configure(
                        values=[
                            f"{row.unique_code} · {row.difficulty} · {row.correct_flag_count}/{row.flag_count} flag"
                            for row in self.rows
                            if not row.is_completed
                        ]
                    )
                    if any(not row.is_completed for row in self.rows):
                        self.challenge_box.current(0)
                elif kind == "session":
                    session, _run = value  # type: ignore[misc]
                    self.current_session = session
                    self.status.set("当前题已创建。点击“查看当前做题过程”打开对应网页。")
                elif kind == "done":
                    self.busy = self.running = False
                    self.progress.stop()
                    for control in self.controls:
                        control.state(["!disabled"])
                    self.stop_button.configure(state="disabled")
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100, self.drain)

    def close(self) -> None:
        if self.busy:
            if self.running:
                self.stop_after_current()
                messagebox.showinfo(
                    "正在等待当前题收尾",
                    "已停止安排后续题目。请在网页取消当前题或等待结束后关闭。",
                    parent=self.root,
                )
            else:
                messagebox.showinfo("正在处理", "请等待当前准备操作结束后关闭。", parent=self.root)
            return
        if self.console.child is not None and self.console.child.poll() is None:
            self.launch("正在关闭本次启动的控制台……", self.console.close)
            self.root.after(200, self.close_when_idle)
            return
        self.closed = True
        self.root.destroy()

    def close_when_idle(self) -> None:
        if self.busy:
            self.root.after(200, self.close_when_idle)
        else:
            if self.console.child is not None and self.console.child.poll() is None:
                messagebox.showerror(
                    "控制台尚未关闭",
                    "请保留窗口并检查关闭错误，避免中断正在收尾的进程。",
                    parent=self.root,
                )
                return
            self.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--smoke-seconds",
        type=float,
        default=0,
        help="只打开空白启动器并自动关闭，不连接平台或调用模型",
    )
    args = parser.parse_args()
    configure_stdio_utf8()
    configure_event_loop_policy()
    root = tk.Tk()
    window = EvaluationWindow(root)
    if args.smoke_seconds > 0:
        root.after(int(args.smoke_seconds * 1000), window.close)
    root.mainloop()


if __name__ == "__main__":
    main()
