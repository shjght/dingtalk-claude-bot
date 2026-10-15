# 钉钉遥控 Claude Code（dingtalk-claude-bot）

手机钉钉发消息 → 电脑上的 Claude Code 干活 → 结果发回手机。
人在外面也能遥控家里的电脑：派任务、批审批、发文件、看进度。

```
手机钉钉 App ──发消息──> 钉钉云端 ──Stream长连接──> 电脑上的 bot.py
                                                      │
                                                  启动 claude 执行任务
                                                      │
手机钉钉 App <──发结果── 钉钉云端 <──────┘
```

电脑**不需要**装钉钉客户端（Stream 模式，无需公网 IP 和端口转发）。

## 功能

- **对话式遥控**：手机直接派任务，支持连续对话（有记忆）、分类会话（`/s` 论文、工作分线互不干扰）
- **手机审批**：白名单外的危险操作转发手机批 ＝ 回 1 允许 / 2 拒绝 / 3 带指示 / 4 同类不再问 / 5 本任务全放行
- **硬拦截**：`rm` / 格式化 / 关机 / `git push --force` 等破坏性命令直接拦，不询问
- **文件双向**：手机发图片/文件给 claude 看；`/file 路径` 把电脑文件发到手机；提到 PDF/Word 等自动推送
- **网页面板**：手机浏览器图形界面（审批卡片、实时输出、token 消耗），配 Tailscale 可外网用，支持 PWA 加到主屏幕
- **实时过程窗口**：`/live` 在电脑上弹窗滚动显示任务的思考/工具/回复
- **会话交接**：`/交接` 把长会话要点沉淀成记忆文件后重开，新会话自动读记忆接续
- **守护**：开机自启 + 计划任务 watchdog，进程悄悄挂掉自动拉活；崩溃留尸检日志
- 进度播报 / `/tail` 看输出 / `/usage` 看消耗 / `/mode` 切权限模式 / `/model` 切模型

## 环境要求

- Windows 10/11（其他系统需自行改启动脚本）
- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录（终端里 `claude` 能跑即可）

## 安装步骤

### 1. 拉代码、装依赖

```powershell
git clone https://github.com/shjght/dingtalk-claude-bot.git
cd dingtalk-claude-bot
pip install -r requirements.txt
```

### 2. 创建钉钉应用（免费，5 分钟）

1. 手机装钉钉，注册登录；可免费创建一个"团队"组织
2. 电脑浏览器打开 https://open-dev.dingtalk.com ，扫码登录
3. 「应用开发」→「创建应用」，名字随意（如 `claude遥控`）
4. 应用内「凭证与基础信息」复制 **AppKey（Client ID）** 和 **AppSecret（Client Secret）**
5. 「应用能力」→ 添加「机器人」→ **消息接收模式必须选「Stream 模式」** → 保存
6. 「版本管理与发布」→ 发布上线（不发布手机里搜不到）

### 3. 填配置

```powershell
copy config.example.json config.json   # macOS/Linux 用 cp
```

编辑 `config.json`，至少填好 `app_key`、`app_secret`；`work_dir` 改成你想让 claude 工作的目录。
其余字段见下方[配置说明](#配置说明)，`panel_token` 留空会在首次启动时自动生成。

### 4. 自检并启动

```powershell
python bot.py --check      # 自检配置
python bot.py              # 前台启动；或双击 start_bot.bat 后台启动
```

### 5. 绑定白名单

1. 手机钉钉搜索你的应用名，点进单聊，随便发一条消息
2. 机器人回复 `⛔ 你（xxxx）不在白名单…`，把那串 ID 填进 `config.json` 的 `allowed_users`
3. 重启，再发 `/help` 看到用法说明就完成了 🎉

### 6.（可选）开机自启 + 防挂守护

- 把 `开机自启_后台.vbs` 的路径改成你的安装路径，放进
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`，登录后自动后台运行（无窗口）
- 注册守护任务（每 2 小时检查，进程不在就拉活）：

```powershell
schtasks /Create /F /TN claude_bot_watchdog /SC MINUTE /MO 120 /TR "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \"你的路径\watchdog.ps1\""
```

日常开关：`start_bot.bat` 启动 / `stop_bot.bat` 停止（会连守护一起暂停）/ `bot_log.bat` 看实时日志。
启动脚本里写死了示例路径，请按你的安装位置修改。

## 配置说明

| 字段 | 说明 |
|---|---|
| `app_key` / `app_secret` | 钉钉应用凭证（第三步复制的） |
| `allowed_users` | 白名单钉钉用户 ID，其他人发消息一律忽略 |
| `work_dir` | claude 执行任务时的工作目录 |
| `task_timeout_minutes` | 单任务超时（默认 30 分钟） |
| `approval_timeout_minutes` | 手机审批等待时长，超时自动拒绝 |
| `progress_minutes` | 任务运行中的进度播报间隔 |
| `max_pending_tasks` | 排队上限 |
| `live_window` | 是否允许 `/live` 弹实时窗口 |
| `model` / `models` | 默认模型与可切换列表（留空用 claude 默认） |
| `permission_mode` | `default` 审批 / `acceptEdits` 自动编辑 / `bypass` 全自动（硬拦截仍生效） |
| `panel_token` | 网页面板令牌，留空自动生成 |

## 安全机制

- 白名单之外的消息全部忽略
- 破坏性命令硬拦截（`bot.py` 顶部 `BASH_DENY` 可增删，`BASH_ALLOW` 是放行白名单）
- 白名单外操作转发手机审批，10 分钟不回自动拒绝，无人值守不会乱来
- **`config.json`、`bot.log`、`sessions/` 含密钥和隐私，已在 `.gitignore` 排除，切勿手动提交或外传**

## 更多文档

- [设置指南.md](设置指南.md) —— 完整图文教程：网页面板、Tailscale 外网访问、文件服务器、分类会话、交接模式、常见问题
- [修改记录.md](修改记录.md) —— 开发 Q&A：19+ 个真实踩坑记录（钉钉 API 的坑、权限应答信封格式、编码问题…）

## 免责声明

本项目仅供个人在自己的电脑与自己的账号上做自动化使用。请自行遵守钉钉开放平台与 Anthropic 的服务条款，
妥善保管密钥，不要把机器人暴露给不受信任的人。
