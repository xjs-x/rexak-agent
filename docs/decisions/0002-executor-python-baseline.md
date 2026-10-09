# 0002 · 执行岛 Python 基线与部署方式

- 状态：**已决定并已执行**
- 日期：2026-10-08
- 关联：decision 0001 §4.4

## 背景

方案要求"根项目采用 uv workspace，将 `executor/` 作为成员；根目录一个 `uv.lock`，
控制端与 Executor 保持独立依赖声明"，并把 Python 锁到 3.12 的实际补丁版。

实际环境：

| 项 | 控制端（Windows） | 执行岛（Kali） |
|---|---|---|
| Python | uv 管理的 CPython **3.12.14** | 系统自带 **3.13.12**（无 3.12 包可装） |
| GitHub 可达 | 是 | **否**（`curl` 连接被重置，HTTP 000） |
| PyPI 可达 | 是 | 是（`pypi.org` 与清华镜像均 200） |
| sudo | 不适用 | **无免密 sudo** |
| systemd | 不适用 | 有，但 `Linger=no` 且无法启用（需要 sudo） |

## 决定

1. **`requires-python` 放宽到 `>=3.12,<3.14`**（根与 `executor/` 都是）。
   - `.python-version` 保持 `3.12`，`uv.lock` 仍按 3.12.14 解析 —— 控制端的开发、
     测试与锁依赖基线**没有改变**；
   - 上限放宽的唯一原因是执行岛只有 3.13，且岛上无法安装独立 3.12 解释器；
   - Executor 不依赖 LangGraph，不需要与控制端保持完全一致的补丁版本。

2. **执行岛用独立 venv 安装 Executor，不通过 uv workspace 部署。**
   原因同上：岛上装不了 uv 需要的独立 CPython。安装方式：

   ```bash
   # 执行岛
   python3 -m venv ~/rexak-agent-executor/.venv
   ~/rexak-agent-executor/.venv/bin/python -m pip install \
     -i https://pypi.tuna.tsinghua.edu.cn/simple ~/rexak-agent-executor/executor
   ```

   `uv.lock` 仍然是控制端的唯一锁文件；执行岛的实际安装版本记录在
   `docs/implementation-status.md`。

3. **执行岛用 `setsid` 脱离会话常驻，而不是 systemd。**
   方案 §2 允许"systemd 或容器"。当前环境无法安装系统级 unit（无免密 sudo），
   用户级 unit 因 `Linger=no` 无法在登出后存活。
   因此采用 `setsid nohup` + PID 文件管理，满足方案的核心要求：
   **SSH channel 断开不销毁进程服务**。

   已提供的脚本（部署在执行岛 `~/rexak-agent-executor/`）：

   | 脚本 | 用途 |
   |---|---|
   | `start.sh` / `stop.sh` | Executor 常驻起停（PID 文件） |
   | `start-target.sh` | 本地可控测试目标起停 |
   | `executor.env` | Executor 配置（含令牌，权限 600） |

   **限制（必须如实记录）**：
   - 机器重启后不会自动拉起 —— 需要 `start.sh` 或补一个 systemd unit；
   - 这不是"已经具备生产级进程守护"，只是满足"SSH 断开不中断"这一条。

## 已验证

- `GET /health` 返回 `executor_id=kali-01`、`protocol_version=1`、`free_bytes≈57 GB`；
- `GET /capabilities` 如实返回 `network_isolation=unavailable` 与
  `command_unattended_allowed=false`；
- SSH 隧道在本机 `127.0.0.1:<临时端口>` 建立成功，经隧道访问 `/health` 正常；
- Executor 对未知字段是 `extra="forbid"`，控制端提交范围载荷必须只带声明过的字段
  （这一点在联调时抓出过一个真实缺陷，见 git 历史中的 `_scope_payload`）。

## 复现步骤

```powershell
# 控制端
uv sync --frozen --all-packages --group dev
uv run workbench db-init
uv run workbench doctor          # 会一并检查执行岛
```

```bash
# 执行岛（如需重建）
cd ~/rexak-agent-executor
python3 -m venv .venv
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ./executor
cp executor.env .env && chmod 600 .env
./start.sh
./start-target.sh
```
