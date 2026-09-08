# M200 Agent

M200 Agent 是一个面向个人单用户场景的 AI Agent 原型。它以 FastAPI、LangGraph 和
Vue 3 为核心，提供 Web 对话、三层记忆、时间化上下文、文档混合检索、NapCat/OneBot v11 QQ 接入、
可选视觉输入，以及仅管理员可创建的 JMComic 持久下载任务。v0.4.x 还提供管理员 QQ 私聊
情感陪伴闭环：多标签情绪分析、支持策略路由、安全边界、用户纠正与反馈、授权关系资料，
以及 Web 管理、导出和分类删除。

这是面向个人使用的 v0.4 版本，不是生产级多用户系统。项目默认仅接受回环访问，不包含
Docker、Redis、Celery、完整 RBAC、OCR、语音、图像 RAG、生产日志平台或云部署配置。

## 当前发布

- 当前版本：`v0.4.7`
- 仓库：<https://github.com/sanhuaz/M200_Agent>

## 功能

- OpenAI 兼容文本模型，多模型配置并按会话切换；模型管理页可设主默认模型并同步现有会话。
- 模型配置可显式开启 `supports_vision`；只有确认支持视觉输入的模型才接受图片请求。
- LangGraph 原生 Function Calling，不使用正则模拟工具调用。
- AnySearch 通过官方远程 MCP Server 接入；默认停用、逐项授权、管理员确认后才允许 Agent 在明确联网意图下调用，
  网络失败时明确降级，不伪造实时来源。
- SQLite 保存会话、消息、用户事实、确认请求和后台任务。
- LangGraph 负责单轮工具编排，最终状态直接从流中取得；会话历史与摘要由业务数据库保存。
- 角色规则、结构化用户事实和向量语义记忆组成三层记忆。
- TXT、Markdown、HTML、文本型 PDF、DOCX 文档解析与结构化切块。
- Chroma 语义召回 + SQLite FTS5/jieba 关键词召回 + RRF 融合。
- 可选在线 Reranker，失败时明确降级到 RRF，不伪装成重排成功。
- BGE 或 OpenAI 兼容在线 Embedding；切换配置时使用影子索引重建。
- NapCat OneBot v11 WebSocket Client 接入、消息去重、群聊触发规则和分组命令帮助。
- 管理员 QQ 私聊默认进入情感陪伴流程；支持多标签情绪分析、倾听/梳理/安慰/建议路由、高风险安全转向，
  以及 `/support`、`/emotion`、`/feedback`、`/companion` 命令。非管理员私聊和群聊保持通用 Agent 行为。
- 陪伴主模型接收 Top-3 情绪候选、支持需要和最终策略作为不确定辅助信号；七种普通策略攻略可在 Web 编辑、
  查看版本、回滚和恢复默认值，角色语气仍由结构化角色卡优先决定。
- 管理员 QQ 私聊支持持久化“倾听模式”，连续短消息可在 30 秒静默或完成命令后合并处理；标准安全预检仍在缓冲前执行。
- QQ 主模型回复在完整生成与复核后按语义自然分批发送，数据库仍保存一条完整助手消息；空白和无文本文件回执在入口直接忽略。
- 标准安全模式下，高风险消息由受限 LLM 结合原文、近期上下文和人格语气生成安全说明；普通回复触发复核时
  只重写一次再降级。已启用的 QQ 管理员可通过 `/companion safety unfiltered confirm` 切换为仅审计、不中断普通
  Agent 的无过滤模式，并用 `/companion status` 查看；模型服务商自身规则仍然有效。
- 关系记忆默认关闭，一次授权后按管理员、人格隔离保存低敏感事实；Web 长期记忆中心统一管理普通事实与陪伴关系，
  `/companion`、`/emotion-records` 和 `/privacy` 支持陪伴设置、纠正、导出与分类删除，旧 `/relationships` 自动跳转到关系筛选。
- JMComic 搜索、管理员直接下载、单 Worker、PDF 产物、QQ 私聊发送与显式清理。
- 动态 Tool Registry：内置 Tool、审核后启用的 Python Tool、文件创建 Tool。
- Agent Skills：`SKILL.md` 导入、启停、自动/手动加载；包内脚本只展示不执行。
- MCP：通过官方 Python MCP SDK 接入 stdio、SSE 和 Streamable HTTP Server；工具、资源、资源模板和 Prompt
  统一进入 Tool Registry，逐项授权后才可调用；v0.4.7 增加全局/Server/Tool 三层自定义意图、确定性优先级路由、
  `可用`/`已连接但未授权`/`不可用` 三态提示、10 分钟紧邻追问继承和完整结果交付。第三方 MCP Server 的实际稳定性取决于其自身实现，并非模型本身。
- Web 和 QQ 支持 JPEG、PNG、GIF、WebP 文图或纯图片消息；每条消息最多 4 张、总大小不超过 32 MiB、
  图片最多 40MP。图片只保存在私有 `data/chat-images/`，不进入 Chroma、RAG 或公开目录。
- 图片输入校验失败时不保存；消息/附件持久化失败会回滚并删除图片；已持久化后模型失败则保留用户消息和图片，
  不写助手消息并允许重试。
- 文件化结构角色卡存放在本机 `persona/`，JSON 文件是正文唯一权威来源，数据库只保留索引和会话引用；Web 与 QQ 会话可独立选择人格或关闭人格。
- 长期记忆中心对外统一展示普通事实、历史事件与陪伴关系，并按全局、QQ 用户、群组和人格筛选；内部仍保留两个独立存储库。
- 数据库以 UTC 保存时间；聊天、摘要、记忆召回和陪伴上下文使用同一配置时区，Web Chat 页面展示消息本地时间、跨日分隔、
  完整时区提示，长期记忆中心展示事件日期、首次记录和最近确认/更新。
- 文件产物隔离、SHA-256 记录、Web 下载及 QQ 私聊发送。
- Vue 3 管理端：聊天、模型管理、知识库、Tools、Skills、人格、长期记忆、管理员、任务和状态页面；网页会话可删除，任务与确认记录支持批量清理。
- 实时日志中心：侧栏 `/logs` 提供 M200 结构化操作日志和 NapCat 原生日志双标签，实时显示启动、模型、Tool、记忆、RAG、索引、任务、下载和 OneBot 状态。

## 架构

```text
Web / QQ
   ↓
FastAPI API + OneBot WebSocket
   ├─ ChatTurnInput（文本与图片附件）
   ├─ MCP Manager（stdio / SSE / Streamable HTTP）
   └─ 管理员 QQ 私聊：陪伴安全预检 →（标准安全转向 / 无过滤审计）→ 情绪分析 → 策略路由 → 关系资料（需授权）
        ↓
LangGraph Agent
   ├─ Chat Model / Function Calling
   ├─ Unified Tool Registry / Skills / MCP
   ├─ Long-term Memory
   ├─ UTC 时间存储 → 配置时区上下文与页面展示
   ├─ Knowledge Retrieval
   │    ├─ Chroma Vector Search
   │    ├─ SQLite FTS5 + jieba
   │    ├─ RRF
   │    └─ Optional Reranker
   └─ Manga Search / Confirmation / Job Worker
        ↓
SQLite（关系、消息、状态） + Chroma（文档/记忆向量） + 私有图片与生成产物
```

### 公开仓库目录架构

以下是 `v0.4.7` 公开源码的目录结构：

```text
M200_Agent/
├─ backend/
│  ├─ alembic/
│  │  ├─ env.py
│  │  └─ versions/                 # 0001—0013，支持旧数据库逐版升级
│  └─ app/
│     ├─ api/                      # 分域 Router、聊天、多模态附件、OneBot、MCP
│     ├─ core/                     # 环境配置
│     ├─ db/                       # SQLAlchemy 模型与 Session
│     ├─ domain/                   # 与存储无关的领域类型
│     ├─ services/                 # 聊天、附件、记忆、RAG、陪伴、扩展、MCP 与任务服务
│     ├─ workflows/                # LangGraph Agent 图
│     └─ main.py                   # FastAPI 生命周期与应用入口
├─ frontend/
│  ├─ src/
│  │  ├─ features/                 # 聊天、MCP、导航、筛选和请求竞态等纯逻辑
│  │  ├─ pages/                    # 页面级异步组件
│  │  ├─ services/                 # API 与 SSE 客户端
│  │  ├─ types/                    # 前端领域类型
│  │  ├─ App.vue                   # 顶层状态和主要工作区
│  │  └─ main.ts
│  ├─ package.json
│  └─ pnpm-lock.yaml
├─ scripts/
│  ├─ start.ps1 / stop.ps1         # 本机启动与安全停止
│  ├─ python-resolver.ps1          # Python 3.13 自动发现与手动选择
│  ├─ migrate.ps1                  # 迁移判断、升级前备份和自动保留
│  ├─ migration_plan.py            # 当前版本与目标 head 比较
│  ├─ backup_inventory.ps1         # 旧备份只读清单
│  └─ langgraph_rag_eval.py        # LangGraph 知识库选路评测方法
├─ data/                            # 仅发布运行目录占位文件
├─ persona/ / skills/ / tools/     # 公开仓库仅保留目录占位文件
├─ .env.example                    # 无真实密钥的配置模板
├─ alembic.ini
├─ pyproject.toml
├─ requirements.txt
├─ requirements-dev.txt
└─ README.md
```

`backend/alembic/versions/` 中的历史迁移不是重复文件。Alembic 需要按修订链将旧版本数据库逐步升级到
`0013_tool_run_user_message`，因此公开发行必须保留 `0001`—`0013`。

## 技术栈

- Python 3.13、FastAPI、SQLAlchemy、Alembic、Uvicorn
- LangChain 1.2、LangGraph 1.1
- MCP Python SDK 2.1.1、Pillow 12.3.0
- SQLite FTS5、ChromaDB、Sentence Transformers、jieba
- Vue 3、TypeScript、Vite、Element Plus
- NapCat OneBot v11、JMComic-Crawler-Python

## 环境准备

工作解释器使用 Python 3.13。启动、迁移、测试和安装依赖都应显式使用项目解释器，
不依赖当前终端的 `python` 指向。

Windows PowerShell 示例：

```powershell
& python -m pip install --no-user -r requirements.txt
```

`requirements-dev.txt` 仅供维护者在本机安装 Ruff、Pyright、Pytest 等开发工具；普通用户运行应用无需安装。

安装前端依赖：

```powershell
Set-Location frontend
pnpm install
Set-Location ..
```

## 配置

复制配置模板：

```powershell
Copy-Item ".env.example" ".env"
```

至少需要配置一个支持原生 Tool Calls 的 OpenAI 兼容模型。下面的 JSON 只在首次启动时导入；项目初始化后，
可在 Web 管理端 `/models` 的“模型管理”页面快捷调整模型名称、`base_url`、思考强度、流式输出、温度和请求限制：

```env
PERSONAL_AGENT_TIMEZONE=Asia/Shanghai
```

数据库和公开 API 以 UTC 为基准；该配置用于模型时间上下文和 Web 页面本地时间展示。健康接口会返回当前配置的时区，
前端据此显示消息时间、跨日分隔和完整时区提示。

```env
MODEL_PROFILES_JSON=[{"alias":"default","model":"your-tool-capable-model","base_url":"https://provider.example/v1","api_key_env":"PERSONAL_AGENT_LLM_API_KEY","reasoning_effort":null,"streaming":true,"temperature":null,"context_window":1000000,"input_soft_limit":131072,"max_output_tokens":16384,"timeout_seconds":120,"supports_vision":false}]
PERSONAL_AGENT_LLM_API_KEY=<provider-api-key>
```

API Key 通过 `api_key_env` 间接引用，不应写进 JSON、源码、日志或 Git。通过模型管理页录入的新密钥只写入
被版本控制忽略的 `.env`，数据库和 `GET /api/v1/models` 不保存或返回密钥。模型配置修改从下一次请求生效；关闭流式
输出时仍使用聊天 SSE 接口，但正文会在生成完成后一次性返回。`reasoning_effort` 按 OpenAI 兼容标准传递，服务商
不支持时应使用“测试连接”确认兼容性。

模型管理页的“设为主默认模型”会把网页、QQ 和归档会话统一切换到已配置模型，并作为后续新会话的默认值；
进行中的请求继续使用开始请求时的配置。`PUT /api/v1/models/{alias}/default` 可执行同样的管理操作。
网页会话删除接口为 `DELETE /api/v1/conversations/{id}`，QQ 会话会被拒绝；删除只清理会话消息、工具轨迹和
工作流状态，长期记忆、任务记录和产物文件保留。任务中心的 `POST /api/v1/tasks/bulk-delete` 只接受已结束任务，
`POST /api/v1/confirmations/bulk-delete` 可删除待确认或已处理的确认记录，删除任务记录不会删除下载文件。

Embedding 可选择：

```env
DEFAULT_EMBEDDING_PROFILE=local-bge
LOCAL_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5

ONLINE_EMBEDDING_MODEL=your-embedding-model
ONLINE_EMBEDDING_BASE_URL=https://provider.example/v1
ONLINE_EMBEDDING_API_KEY_ENV=PERSONAL_AGENT_EMBEDDING_API_KEY
PERSONAL_AGENT_EMBEDDING_API_KEY=<embedding-api-key>
```

可选 Reranker：

```env
RERANK_ENABLED=false
RERANK_URL=https://api.siliconflow.cn/v1/rerank
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=<reranker-api-key>
```

QQ 管理命令需要配置管理员。管理员应是给机器人发送消息的个人 QQ，而不是机器人自身账号：

```env
ONEBOT_TOKEN=<onebot-token>
OWNER_QQ_IDS=["<owner-qq-id>"]
```

`.env` 已被 `.gitignore` 排除。

实时日志页的 NapCat 配置也可以写入 `.env`：

```env
NAPCAT_WEBUI_URL=<napcat-webui-url>
NAPCAT_WEBUI_TOKEN=<napcat-webui-token>
```

推荐直接在 `/logs` 页面填写地址和 Token，后端使用 NapCat 官方 WebUI 登录并订阅实时 SSE 日志。地址必须是
回环地址；Token 只写入被版本控制忽略的 `.env` 并同步当前进程环境，接口、页面、运行日志和归档日志都不会返回或记录 Token。NapCat 启用 2FA 时，页面会提示不支持自动续期。

## MCP 外部工具与服务

在 Web 的 `/mcp` 设置页管理 MCP Server。每个 Server 默认停用、授权为空、访问策略为 `owner_only`，
管理员确认后才会把目录中的项目加入统一 Tool Registry：

- `stdio` 适合本机进程；`SSE` 和 `Streamable HTTP` 适合本机或受控网络服务。
- 支持发现和按需调用 Tools、Resources、Resource Templates、Prompts。Resources 和 Prompts 不会自动注入系统提示词，
  由桥接工具按需读取，文本返回模型，二进制内容保存为本机 Artifact。
- Server 配置只保存非敏感参数；密钥写入 `.env`，SQLite 和 API 只保留环境变量引用及“已配置”状态。
- 可逐项设置 Tool、Resource、Template 和 Prompt 白名单；MCP 名称使用 `mcp__<server_slug>__<item_name>`，不会覆盖内置工具。
- 单个第三方 Server 的连接、目录或调用失败只标记该 Server，不阻断应用启动、普通聊天或其他 Server。
- 固定风险说明：第三方 MCP Server 的实际稳定性取决于其自身实现，并非模型本身。

管理端对应接口为 `GET/POST /api/v1/mcp/servers`、`PUT/DELETE /api/v1/mcp/servers/{id}`、
`PUT /api/v1/mcp/servers/{id}/enabled`、`POST .../test`、`POST .../refresh`、`GET .../catalog` 和
`PUT .../grants/{kind}`。本项目不实施 OAuth 交互登录、Sampling、Elicitation、Roots 或订阅。

### AnySearch 联网搜索

MCP 设置页提供 AnySearch 预设，使用官方远程 Streamable HTTP 服务（`https://api.anysearch.com/mcp`）。预设默认停用、
访问策略为 `owner_only` 且不授予任何工具；可选择匿名访问，或在安装时将 API Key 写入本机 `.env`，SQLite、API、页面和日志
只显示环境变量引用或已配置状态。Agent 只有在用户明确要求搜索、当前信息或事实核查时才会调用；垂直搜索先发现合法
`sub_domain`，联网失败、超时或异常结果时明确降级，不把旧知识或猜测伪装成实时结果。

## 启动

分别启动后端和前端时，先确保当前终端可找到 Python 3.13：

```powershell
Set-Location backend
& python -m uvicorn app.main:app
```

```powershell
Set-Location frontend
pnpm run dev
```

也可以从项目根目录运行：

```powershell
.\scripts\start.ps1
```

启动脚本默认使用后端端口 `8000`。如果该端口被 Windows 排除范围占用且 `8200` 可用，脚本会自动回退到 `8200`，
并同步更新本次 Vite `/api` 代理；终端输出的实际后端地址为准。也可以用 `-BackendPort`、`-FrontendPort` 显式指定端口。

启动脚本按以下顺序寻找解释器，并逐一验证 `sys.executable` 和 Python 版本：
`-PythonExe` 参数、`PERSONAL_AGENT_PYTHON`、项目 `.venv\Scripts\python.exe`、`py -3.13`、`PATH` 中的 `python`，
最后才在交互终端中要求手动输入完整路径。例如：

```powershell
$PythonExe = (Get-Command python -CommandType Application).Source
.\scripts\start.ps1 -PythonExe $PythonExe
# 或
$env:PERSONAL_AGENT_PYTHON = (Get-Command python -CommandType Application).Source
.\scripts\start.ps1
```

依赖安装也应使用同一解释器。维护者检查使用 `requirements-dev.txt`，普通用户只需安装 `requirements.txt`。

停止由项目脚本启动的前后端：

```powershell
.\scripts\stop.ps1
```

停止脚本只会结束经命令行和程序路径确认属于本项目的监听进程；遇到未知进程占用
端口时会拒绝执行，避免误杀其他项目。

`start.ps1` 为每次启动创建 `logs/current/<session-id>/` 会话目录；后端会将最近 2000 条事件写入内存并以
UTF-8 JSONL 分片持久化，单分片达到 50 MiB 自动轮换。运行 `stop.ps1` 时，后端先收到停止刷新请求；两个项目端口
均释放且 ZIP 校验成功后，当前会话才会压缩到 `logs/archives/m200-agent-<开始时间>-<会话ID>.zip`，原始目录随后删除。
历史 ZIP 永久保留并由用户手动清理；归档失败会保留原始日志。日志允许记录完整 QQ 号、消息、RAG 查询、Tool
参数、文件路径和错误堆栈，但 API Key、NapCat Token、Authorization、密码、Cookie 等凭据始终强制脱敏，单条事件
超过 64 KiB 会截断并标记；模型不逐 Token 记录，二进制内容不写入日志。

访问入口：

- Web：管理端地址
- OpenAPI：后端地址/docs
- 健康检查：后端地址/api/v1/health

## NapCat OneBot v11

在 NapCat WebUI 中创建并启用 WebSocket Client：

```text
URL: ws://<backend-host>/<onebot-path>
Token: 与 .env 中 ONEBOT_TOKEN 完全一致
心跳间隔: 30000
重连间隔: 5000
消息格式: Array
```

规则：

- 私聊默认响应。
- 群聊仅响应 `@机器人` 或 `/ai` 前缀。
- `/help` 按会话与模型、知识库与记忆、人格、Tools 与 Skills、漫画、确认分组显示完整命令。
- `/new`、`/reset-context`、`/context`、`/model list`、`/kb`、`/memory`、`/jm` 可用于查询或管理会话。
- 管理员私聊可使用 `/support auto|listen|reflect|advice`、`/emotion`、`/emotion correct <情绪标签>`、
  `/feedback helpful|unhelpful|no-advice`、`/companion pause|resume` 和 `/companion memory on|off`；
  还可使用 `/companion safety standard`、`/companion safety unfiltered confirm` 和 `/companion status`；
  合法情绪标签会在无效输入时提示，纠正不会覆盖原始分析。
- 陪伴流程只对已启用管理员的私聊生效；暂停后回到通用 Agent，群聊和非管理员不创建陪伴数据。
- 记忆授权关闭时陪伴私聊不召回或新增普通长期记忆/关系资料；标准模式高风险内容停止普通角色扮演和工具调用，
  无过滤模式只记录风险并继续普通 Agent 全链路，但仍保留权限、Tool 确认、文件隔离和密钥脱敏。
- 会话上下文采用增量摘要和 Token 预算；`/new` 只归档短期对话，不删除长期记忆。群聊共享群上下文，但不会召回成员私聊记忆。
- `/tools`、`/skills`、`/skill <name> <request>` 可查看和手动触发已启用扩展。
- `/model use`、漫画下载/删除和管理操作仅管理员可用。Tool/Skill 管理仍使用 `/confirm` 二次确认。
- 管理员私聊的 `/listening` 命令继续兼容“你听我说”自然语言触发，产品显示统一为“倾听模式”；图片和文字会共同缓冲，
  完成或静默后合并为一个多模态轮次。
- QQ 图片支持文图、纯图片和最多 4 张多图；模型未启用 `supports_vision` 或图片校验失败时，在写入前返回错误。
- `/jm download <漫画ID>` 立即创建下载任务；QQ 发送成功后可用 `/jm delete <任务ID>` 删除产物。
- 健康检查中的 OneBot 状态分为 `connected`、`configured_disconnected`、
  `needs_configuration`。
- `/logs` 页会分别显示 NapCat 进程日志流、QQ `get_login_info` 在线状态和 OneBot WebSocket 连接，三者不互相替代。
- 日志接口：`GET /api/v1/logs`、`GET /api/v1/logs/active`、`GET /api/v1/logs/stream`；NapCat 配置使用
  `GET/PUT /api/v1/logs/config`，草稿连接测试使用 `POST /api/v1/logs/test-connection`，停止脚本使用
  `POST /api/v1/logs/finalize`，这些管理接口仅接受回环请求。

## Web 识图

聊天页 `/chat` 支持选择、预览、发送和删除 JPEG、PNG、GIF、WebP。文本为空但有图片时可以发送，
每条消息最多 4 张、总大小不超过 32 MiB、总像素不超过 40MP；服务器会再次使用 Pillow 检查真实格式和像素。
含图片请求使用 `multipart/form-data` 的 `/api/v1/chat/stream/multimodal`，旧的 JSON `/api/v1/chat/stream` 保持兼容。

图片保存在私有 `data/chat-images/`，数据库只保存附件元数据和相对路径，不进入文档索引、Chroma 或 RAG。
消息和附件持久化成功后，如果模型或 Agent 超时，用户消息和图片会保留，不写助手消息，页面通过 SSE 返回失败并可直接重试。
删除附件时先原子移动到同目录临时回收名，再删除数据库记录；数据库提交失败会恢复原路径，提交成功后才彻底删除文件。

## 文档检索

查询流程：

```text
Chroma 语义召回 20 条
+ SQLite FTS5 关键词召回 20 条
→ RRF 合并去重
→ 可选 Reranker
→ 最终 5 条证据
→ LLM 带文件、标题和页码或段落位置回答
```

扫描 PDF 不做 OCR。切换知识库 Embedding 时，新集合全部构建成功后才切换；失败时继续使用
旧索引，禁止混合不同模型或维度的向量。

## 漫画任务边界

- 搜索不会自动下载。
- 只有管理员可以下载；管理员发起后立即创建任务，不再要求二次确认。
- Worker 单并发，瞬时失败最多重试两次，默认只生成 PDF。
- QQ 仅向管理员私聊自动发送；超过阈值或发送失败时返回产物位置。
- 成功发送后，管理员可通过 QQ 命令或 Web 任务页显式删除该任务的产物；任务审计记录保留。
- 用户应确保对下载内容拥有合法保存权利。

## 扩展边界

- Tool ZIP 必须包含 `tool.json` 和 `plugin.py:create_tools`，导入后默认停用，不自动安装依赖。
- Skill ZIP 必须包含与目录同名的 `SKILL.md`；启动只读取名称/描述，完整内容按需加载。
- MCP Server 默认停用，使用 `owner_only` 访问策略和逐项白名单；连接由 MCP Manager 管理，调用通过统一 Tool Registry。
- 人格使用结构化角色卡并以 `persona/` 中的 JSON 文件为正文唯一来源；数据库只保留索引、状态和会话引用，
  角色卡不能修改系统规则、工具、权限、记忆范围、密钥或文件根目录。
- 文件只能写入受控的生成目录，不自动执行；多文件自动 ZIP。
- `local-owner` 永久存在不可删除，QQ 管理员由数据库实时管理；`.env` 中的 `OWNER_QQ_IDS` 仅首次迁移导入。

## 版本更新记录

### v0.4.7

- MCP 意图从仅依赖“搜索”关键词扩展为确定性路由：支持动态 Server/Tool 名称、全局/Server/Tool 自定义短语、AnySearch 当前事实规则、同级歧义阻断和无意图工具关闭。
- MCP 调用前统一计算 `可用`、`已连接但未授权`、`不可用` 三态；同一决定同时约束系统提示、工具注册、调用时授权、日志和结果交付。
- 新增 `0013_tool_run_user_message`，精确关联 ToolRun 与用户回合；紧邻追问在 10 分钟内按会话、用户和已完成回合继承短期 MCP 证据，刷新请求仍重新调用。
- 成功且非空的 MCP 结果进入功能回复通道，保留完整正文并绕过陪伴风格与短回复长度修订；安全检查仍执行。结果为空或失败时不生成成功 Markdown，也不以工具未返回的内容伪装实时结果。
- 用户要求文件、正文超过 600 个中文字符或实际包含不少于 5 项列表时，从同一完整正文生成 Markdown Artifact；Web SSE 提供下载信息，QQ 私聊先发送语义完整文本，再在文件实际发送成功后记录送达状态；同一 Artifact 按 ID 去重，避免重复发送。
- MCP 选中但返回空/失败结果，或目标处于未授权/不可用状态且没有真实结果时，拦截模型伪成功话术并返回明确状态；空模型输出进入错误流；成功 MCP 助手正文不复制进轮后记忆、关系提取或长期会话摘要，仍保留在普通会话历史和 10 分钟短期回合证据中。
- `final` 与 `artifact_created` 事件保留旧字段并追加可选 MCP/Artifact 元数据；MCP 审计日志不写入外部正文，只记录状态、长度和标识。

### v0.4.6

- 增加 AnySearch 官方远程 MCP 预设，支持匿名或 API Key 安装；默认停用、零授权、管理员确认后启用，Agent 按明确联网意图调用，
  垂直搜索先发现合法 domain，搜索失败时明确降级并保留来源边界。
- 增加短回复预算和语义完整性校验：普通 Web/QQ 回复默认控制为 1–3 个完整短段，URL、代码、命令、路径和完整内容保持原子性；
  长文、教程、报告和完整代码仍进入长模式。
- 修复回复策略修订失败时丢失模型原文的问题：非空原始回复始终保留，空模型输出进入错误流；策略状态区分接受、修订和原文保留，
  QQ 降级回复按完整原文安全分段，日志区分模型输出长度、最终发送长度和策略降级状态。
- 增加统一时间上下文：数据库继续保存 UTC，API 输出带 `Z` 的 UTC ISO 8601；模型上下文、摘要、陪伴、安全和记忆提取使用配置时区，
  默认 `Asia/Shanghai`。
- 增加 `0012_temporal_context` 迁移、摘要格式版本、`fact/event` 记忆类型和明确事件日期；旧摘要标记为版本 1，旧记忆保守默认为
  `fact/null`，不在迁移中猜测历史日期，事件召回按历史背景处理。
- Web Chat 展示消息本地时间、跨日分隔线和完整时区提示；长期记忆中心展示事件日期、首次记录和最近确认/更新，并兼容缺失的旧事件日期。

### v0.4.5

- 统一产品文案：用户可见的“你听我说模式”改为“倾听模式”，`Owner` 显示为“管理员”或“QQ 管理员”，内部权限字段保持兼容。
- 三个启动脚本共用 Python 3.13 解析器，按显式参数、`PERSONAL_AGENT_PYTHON`、项目 `.venv`、`py -3.13`、`PATH` 和手动输入发现，
  删除维护者机器绝对路径回退。
- 增加 MCP Server 管理与统一工具注册，支持 stdio、SSE、Streamable HTTP、目录发现、逐项授权和故障隔离。
- 增加 Web、QQ 和倾听模式的图片输入，支持 JPEG、PNG、GIF、WebP、4 张/32 MiB/40MP 限制、视觉能力开关、私有附件存储和删除补偿，
  不把图片写入 RAG 或 Chroma。模型失败时保留已持久化的用户消息和图片，允许重试。
- 前端新增独立 `ChatPage.vue`、`McpPage.vue`、聊天/MCP feature 与 type 模块，保留旧文本 SSE/API 兼容，并形成页面级分包。

### v0.4.4

- 修复迁移脚本写死旧版本导致每次启动重复备份的问题：动态比较 `alembic current` 与 `heads`，只在确需升级时备份，自动迁移备份默认保留最近 3 份；人工快照和 NapCat 原始备份不自动删除。
- 统一长期记忆、Tool/Skill 扩展和 QQ 命令的领域服务路径，消除 Web 与 QQ 删除记忆时 SQLite/Chroma 行为不一致，以及启停、删除扩展的重复实现。
- 按会话、模型、知识库、记忆、扩展、任务和监控拆分 FastAPI Router；将 OneBot 连接传输、消息解析、文件投递，以及聊天轮后任务和编排辅助逻辑拆成独立模块，保持原 URL、SSE 和 QQ 命令不变。
- 普通聊天不再持久化无恢复用途的 LangGraph Checkpoint；会话历史继续由消息和摘要保存，同时将后台线程数据库操作改为在线程内创建独立 Session。
- 将角色卡领域类型与关系资料存储独立，新增 `0009_remove_legacy_persona_payload`，移除数据库中长期为空的旧人格正文列；历史迁移继续完整保留，支持旧数据库升级。
- 前端拆分导航、请求竞态、记忆筛选、API/SSE 与管理类型模块；管理员、状态和任务中心改为异步页面组件，保持现有交互和 URL。

### v0.4.3

- 陪伴回复完整性升级：主模型接收 Top-3 情绪候选、强度、置信度、支持需要和最终策略；增加七种可编辑策略攻略、版本历史、回滚与恢复默认值。
- 增加结构化角色卡和运行时风格复核，重点处理明确拒绝建议后的建议越权，以及非建议策略中的标题和明显列表；失败时按问题类型安全降级。
- 增加 Owner QQ 私聊“你听我说”模式：普通片段持久化缓冲，30 秒静默或完成命令后合并为一个用户轮次；高风险预检仍优先执行。
- OneBot 入口拦截空白、纯空格和无文本文件回执，不创建消息，也不进入模型、Tool、RAG 或记忆链路。
- QQ 主模型回复在完整生成和复核后自然分批发送；支持全局持久化开关、每会话发送锁、最多 12 个片段及失败即停，Web SSE 不受影响。
- 新增长期记忆中心，将普通事实与陪伴关系统一查询和展示；支持类型、作用域、人格、状态和关键词组合筛选，保留原有独立存储与写接口。
- 角色卡改为项目根目录 `persona/` 下的文件化 JSON；正文从文件实时读取，数据库仅保留索引与引用，损坏文件会停用且可在修复后恢复。

### v0.4.2

- 新增 Owner QQ 私聊情感陪伴编排、结构化多标签情绪分析、安全预检/转向、策略路由和输出复核。
- 新增 `0006_companion_p0` 迁移、陪伴偏好/关系/分析/反馈/安全五张表及管理 API；记忆授权关闭时不召回、不写入。
- 新增 QQ 陪伴命令和 Vue 管理页面 `/companion`、`/relationships`、`/emotion-records`、`/privacy`，支持纠正、乐观锁、导出与幂等分类删除。
- 新增 `0007_companion_safety_mode` 迁移、标准安全回应 LLM/输出复写一次降级链路，以及 Owner 专属无过滤审计模式和 Web/QQ 切换。
- 生产结构化链路在 170 条固定日常场景评测中实现 Top-1 65.88%、Top-3 87.65%、Micro-F1 0.7024、
  支持需求准确率 61.18%和策略准确率 56.47%（96/170）。

### v0.4.1

- 新增实时日志中心与 `/logs` 双标签页，覆盖启动、对话、模型、Tool、记忆、RAG、任务、索引、漫画下载和 OneBot 操作事件。
- 操作事件同时维护最近 2000 条内存快照、活动操作状态和 UTF-8 JSONL 会话文件，支持 SSE 实时推送、筛选、暂停显示和下载进度。
- 接入 NapCat WebUI 实时 SSE 日志与 Token 配置，限制为回环地址并支持凭据续期、断线退避及 QQ 在线状态检查。
- API Key、NapCat Token、Authorization、密码和 Cookie 等凭据递归脱敏；超长事件截断，模型正文不按 Token 逐条记录。
- `start.ps1` 创建日志会话，`stop.ps1` 在安全停止并确认端口释放后将日志校验归档为 ZIP，归档失败时保留原始文件。

### v0.4.0

- 管理端重构为中文侧边栏、顶部状态栏和卡片化工作区，支持亮暗主题、桌面折叠侧栏与窄屏抽屉。
- 新增主聊天模型管理，可维护 OpenAI 兼容配置、测试连接、设置全局默认模型并从下一次请求实时生效。
- 网页会话支持安全删除，QQ 会话受后端保护；聊天加载和发送后默认定位到最新消息，用户上翻时不强制跟随。
- 任务中心支持批量删除确认记录和已结束任务；活动任务原子拒绝删除，下载文件不会随记录删除。

### v0.3.2

- QQ `/help` 改为分组多行输出，覆盖会话、模型、知识库、记忆、人格、Tools、Skills、漫画和确认命令。
- 新增 `scripts/stop.ps1`，按固定端口定位前后端，并在校验进程命令行和程序路径后安全停止。
- 停止脚本支持服务未运行时重复执行；发现未知进程占用项目端口时整体拒绝停止，避免误杀。

### v0.3.1

- 漫画工具改为显式意图门禁：普通聊天不再把漫画工具交给 LLM 自由选择。
- 程序按当前消息识别搜索、下载和删除意图，只临时开放对应的漫画工具；未授权动作无法执行。
- 支持上一轮自然语言漫画搜索产生非空结果后的“下载第一个”等紧邻追问，隔轮或无结果不会继承授权。
- 保留 QQ `/jm` 命令和 Web 漫画搜索、下载按钮的直接调用路径，不改变现有 Owner 权限校验。

### v0.3.0

v0.3 面向长期运行的个人 Agent 场景，重点完善上下文管理、记忆隔离、知识库索引体验和多知识库
LangGraph 链路评测。

#### 长上下文与会话记忆

- 按 DeepSeek V4 Flash 配置 1,000,000 Token 硬上限；日常输入软上限 131,072 Token，单轮输出上限
  16,384 Token。
- 新增统一上下文预算构建器，按当前问题、系统约束、近期消息、历史摘要、长期记忆和低优先级扩展
  逐级装配，不再固定只取最近若干条消息。
- 工具结果增加单次和单轮总量限制；RAG 证据按完整 Chunk 裁剪并保留来源信息。
- 新增增量会话摘要：历史达到约 64K Token 或 40 条消息时触发，始终保留最近 12 条原文；摘要失败
  不移动边界，也不删除历史。
- 长期记忆按全局、QQ 用户和 QQ 群组分域隔离；私聊个人记忆不会注入群聊。
- QQ 新增 `/new`、`/reset-context` 和 `/context` 命令，支持归档短期会话、查看上下文用量和摘要状态。
- 人格保存后直接保留原始提示词；Web 与 QQ 会话可独立选择人格或关闭人格，不再调用 LLM 编译，也不保留全局人格分配。

#### 知识库与 LangGraph 链路

- 知识库索引状态支持前端实时刷新，`queued`、处理中、成功和失败状态无需再次操作页面即可更新。
- Web 对话框保留模型 `default` 下拉框，并新增独立的人格选择下拉框；人格切换只影响当前会话及后续消息。
- QQ 新增 `/persona`、`/persona list`、`/persona use <名称或ID>` 和 `/persona off`；私聊用户可切换自己的会话，群聊仅 Owner 可切换共享人格。
- LangGraph 知识检索增加停止与去重策略：相同“查询 + 知识库 ID”每轮只执行一次，每轮最多执行 4
  次不同知识检索；重复或达到上限后强制模型基于已有证据生成最终回答。
- 同一问题仍允许分别检索 `AI学习` 和 `ALLinRAG`，不会因为知识库名称不同而直接判错。

#### 150 题评测结果

评测题集包含高频题 60 道、长尾题 60 道和模糊题 30 道，实际运行完整 LangGraph 检索链路。判分以
证据相关性和回答质量为准：`ALLinRAG` 与 `AI学习` 存在有效重合时，`ALLinRAG` 命中可以通过。

- 完成：150/150；运行错误：0。
- 检索相关性：3.793/4；检索充分性：3.793/4。
- 回答正确性：3.813/4；回答忠实性：3.820/4；引用质量：2.940/4。
- 端到端平均分：3.800/4；端到端通过率：94.67%。
- 证据判定选路失败：8/150（5.33%）；明确判定为有效重合命中：7/150（4.67%）。
- `ALLinRAG` 被检索：133/150 题；没有按知识库名称机械判错。
- 新停止策略覆盖的 21 道题中，实际检索均值 3.143 次，最大 4 次。

### v0.2.0

- 动态 Tools、Agent Skills、结构化人格、管理员管理、用户级记忆隔离和文件产物。
- NapCat WebSocket、QQ 回复、JMComic 下载及发送后显式清理流程。
