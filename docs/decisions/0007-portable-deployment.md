# 0007 · 独立开发环境与首次部署

- 日期：2026-10-17
- 状态：已实现；Kali 独立目录首次/重复安装实机演练通过，不包含系统软件及虚拟机安装。

团队共享源码与锁文件，每人独立运行 Windows 控制端、本机 PostgreSQL 和 Kali。GitHub 协作不要求机器采用相同 IP、用户名或源码目录，不复制维护者凭据及运行数据。

1. `Settings`的 Kali 地址默认留空，由个人 `.env`或环境变量填写；不回退到维护者的 IP。
2. 控制端数据和备份默认位于当前仓库；远端安装及数据路径的 `~/`按 SSH 登录用户 home 展开，安装路径与数据路径分离。
3. `setup_workbench.py configure`生成个人配置及独立令牌；独占新建文件，禁止覆盖已有 `.env`。SSH 密钥和主机指纹由每位成员独立准备，不自动信任未知主机。
4. `setup_workbench.py install`串联数据库迁移、前端安装构建、Executor wheel 和首次 venv 安装，再调用既有启动器检查；不启动 Runner、模型或测试目标。
5. 首次安装需预备兼容 Python、venv、Chromium 和 SSH。脚本不执行 sudo、不修改系统解释器，也不在缺少依赖时冒充安装通过。可通过配置选择兼容 Python 可执行文件。
6. 新成员在自己的指定 Kali 采用 trusted 执行，沿用 decision 0006 的能力和隔离边界；控制端凭据不上传给 Executor。重复运行不重新生成令牌；不完整环境、身份冲突及活动工作明确报错，保留数据。
7. 日常入口保持 `start-workbench.cmd → scripts/start_workbench.py`；维护者已有 `.env`不重写。
8. 可通过个人配置选择公开 HTTPS 包源，只影响依赖安装命令，不改变 Kali 全局 pip 配置；拒绝 URL 凭据和查询参数。包版本和哈希仍由仓库锁定。

操作指南见[新成员部署](../team-setup.md)，验证见[本轮记录](../acceptance-results/2026-10-17/team-setup/README.md)。旧部署文档中的个人地址/路径只描述历史环境，不是团队约束。
