# M200 Agent

M200 Agent 是一个面向个人、本机单用户场景的 AI Agent 原型。它以 FastAPI、LangGraph 和
Vue 3 为核心，提供 Web 对话、三层记忆、文档混合检索、NapCat/OneBot v11 QQ 接入，以及
仅 Owner 可创建的 JMComic 持久下载任务。

这是面向个人本机使用的 v0.3 版本，不是生产级多用户系统。项目默认绑定本机回环地址，不包含
Docker、Redis、Celery、完整 RBAC、OCR、语音/视觉模型、生产日志平台或云部署配置。

## 当前发布

- 当前版本：`v0.3.1`
- 仓库：<https://github.com/sanhuaz/M200_Agent>

## 功能

- OpenAI 兼容文本模型，多模型配置并按会话切换。
- LangGraph 原生 Function Calling，不使用正则模拟工具调用。
- SQLite 保存会话、消息、用户事实、确认请求和后台任务。
- LangGraph SQLite Checkpointer 保存工作流中断与恢复状态。
- 角色规则、结构化用户事实和向量语义记忆组成三层记忆。
- TXT、Markdown、HTML、文本型 PDF、DOCX 文档解析与结构化切块。
- Chroma 语义召回 + SQLite FTS5/jieba 关键词召回 + RRF 融合。
- 可选在线 Reranker，失败时明确降级到 RRF，不伪装成重排成功。
- 本地 BGE 或 OpenAI 兼容在线 Embedding；切换配置时使用影子索引重建。
- NapCat OneBot v11 WebSocket Client 接入、消息去重和群聊触发规则。
- JMComic 搜索、Owner 直接下载、单 Worker、PDF 产物、QQ 私聊发送与显式清理。
- 动态 Tool Registry：内置 Tool、审核后启用的 Python Tool、文件创建 Tool。
- Agent Skills：`SKILL.md` 导入、启停、自动/手动加载；包内脚本只展示不执行。
- 原始提示词人格管理；Web 与 QQ 会话可独立选择人格或关闭人格；长期记忆按全局、QQ 用户和群组隔离，并由 Owner 管理。
- 文件产物隔离、SHA-256 记录、Web 下载及 QQ 私聊发送。
- Vue 3 管理端：聊天、知识库、Tools、Skills、人格、长期记忆、管理员、任务和状态页面。

## 架构

```text
Web / QQ
   ↓
FastAPI API + OneBot WebSocket
   ↓
LangGraph Agent
   ├─ Chat Model / Function Calling
   ├─ Dynamic Tool Registry / Skills
   ├─ Long-term Memory
   ├─ Knowledge Retrieval
   │    ├─ Chroma Vector Search
   │    ├─ SQLite FTS5 + jieba
   │    ├─ RRF
   │    └─ Optional Reranker
   └─ Manga Search / Confirmation / Job Worker
        ↓
SQLite + Chroma + Local Artifacts
```

## 技术栈

- Python 3.13、FastAPI、SQLAlchemy、Alembic、Uvicorn
- LangChain 1.2、LangGraph 1.1、`langgraph-checkpoint-sqlite`
- SQLite FTS5、ChromaDB、Sentence Transformers、jieba
- Vue 3、TypeScript、Vite、Element Plus
- NapCat OneBot v11、JMComic-Crawler-Python

## 环境准备

工作解释器固定为 `D:\miniconda\envs\langchain1.2\python.exe`（Python 3.13）。启动、迁移、
测试和安装依赖都显式使用该解释器，不依赖当前终端的 `python` 指向。

Windows PowerShell 示例：

```powershell
& "D:\miniconda\envs\langchain1.2\python.exe" -m pip install --no-user -r requirements.txt
```

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

至少需要配置一个支持原生 Tool Calls 的 OpenAI 兼容模型：

```env
MODEL_PROFILES_JSON=[{"alias":"default","model":"your-tool-capable-model","base_url":"https://provider.example/v1","api_key_env":"PERSONAL_AGENT_LLM_API_KEY","context_window":1000000,"input_soft_limit":131072,"max_output_tokens":16384,"timeout_seconds":120}]
PERSONAL_AGENT_LLM_API_KEY=your-key
```

API Key 通过 `api_key_env` 间接引用，不应写进 JSON、源码、日志或 Git。

Embedding 可选择：

```env
DEFAULT_EMBEDDING_PROFILE=local-bge
LOCAL_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5

ONLINE_EMBEDDING_MODEL=your-embedding-model
ONLINE_EMBEDDING_BASE_URL=https://provider.example/v1
ONLINE_EMBEDDING_API_KEY_ENV=PERSONAL_AGENT_EMBEDDING_API_KEY
PERSONAL_AGENT_EMBEDDING_API_KEY=your-key
```

可选 Reranker：

```env
RERANK_ENABLED=false
RERANK_URL=https://api.siliconflow.cn/v1/rerank
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=your-key
```

QQ 高成本命令需要配置 Owner。Owner 应是给机器人发送消息的个人 QQ，而不是机器人自身账号：

```env
ONEBOT_TOKEN=generate-a-long-random-token
OWNER_QQ_IDS=["your-owner-qq"]
```

`.env` 已被 `.gitignore` 排除。

## 启动

分别启动后端和前端：

```powershell
Set-Location backend
& "D:\miniconda\envs\langchain1.2\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
Set-Location frontend
pnpm run dev
```

也可以从项目根目录运行：

```powershell
.\scripts\start.ps1
```

访问地址：

- Web：http://127.0.0.1:5176
- OpenAPI：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/health

## NapCat OneBot v11

在 NapCat WebUI 中创建并启用 WebSocket Client：

```text
URL: ws://127.0.0.1:8000/api/v1/onebot/ws
Token: 与 .env 中 ONEBOT_TOKEN 完全一致
心跳间隔: 30000
重连间隔: 5000
消息格式: Array
```

规则：

- 私聊默认响应。
- 群聊仅响应 `@机器人` 或 `/ai` 前缀。
- `/help`、`/new`、`/reset-context`、`/context`、`/model list`、`/kb`、`/memory`、`/jm` 可用于查询或管理会话。
- 会话上下文采用增量摘要和 Token 预算；`/new` 只归档短期对话，不删除长期记忆。群聊共享群上下文，但不会召回成员私聊记忆。
- `/tools`、`/skills`、`/skill <name> <request>` 可查看和手动触发已启用扩展。
- `/model use`、漫画下载/删除和管理操作仅 Owner 可用。Tool/Skill 管理仍使用 `/confirm` 二次确认。
- `/jm download <漫画ID>` 立即创建下载任务；QQ 发送成功后可用 `/jm delete <任务ID>` 删除本地产物。
- 健康检查中的 OneBot 状态分为 `connected`、`configured_disconnected`、
  `needs_configuration`。

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
- 只有 Owner 可以下载；Owner 发起后立即创建任务，不再要求二次确认。
- Worker 单并发，瞬时失败最多重试两次，默认只生成 PDF。
- QQ 仅向 Owner 私聊自动发送；超过阈值或发送失败时返回本地绝对路径。
- 成功发送后，Owner 可通过 QQ 命令或 Web 任务页显式删除该任务的本地产物；任务审计记录保留。
- 用户应确保对下载内容拥有合法保存权利。

## 扩展边界

- Tool ZIP 必须包含 `tool.json` 和 `plugin.py:create_tools`，导入后默认停用，不自动安装依赖。
- Skill ZIP 必须包含与目录同名的 `SKILL.md`；启动只读取名称/描述，完整内容按需加载。
- 人格原始提示词保存后直接用于选中会话；保存时执行长度和提示注入校验，运行时由系统规则包裹，不能修改
  系统规则、工具、权限、记忆范围、密钥或文件根目录。
- 文件只能写入 `workspace/generated/<user-id>/<artifact-id>/`，不自动执行；多文件自动 ZIP。
- `local-owner` 永久存在不可删除，QQ Owner 由数据库实时管理；`.env` Owner 仅首次迁移导入。

## 验证

```powershell
.\scripts\check.ps1
```

检查包括：

- `pip check`
- Python 字节码编译
- Ruff
- Pyright
- Pytest
- Vue TypeScript 检查
- Vite 构建

当前前端仍集中在单个 `App.vue` 中，通过页面路径切换管理标签，并未拆成 Vue Router 多组件工程。

## 安全与发布边界

公开仓库不包含：

- `.env` 和任何 API Key、Token、QQ 号；
- SQLite 数据库、Chroma 索引、上传文档和下载产物；
- 模型缓存、运行日志、真实测试结果和本机绝对路径；
- NapCat 本机配置。

应用默认仅监听 `127.0.0.1`。如需暴露到局域网或公网，应另行增加身份认证、权限控制、TLS、
限流和完整审计，不应直接复用当前 V1 配置。

## 版本更新记录

### v0.3.1（当前）

- 漫画工具改为显式意图门禁：普通聊天不再把漫画工具交给 LLM 自由选择。
- 程序按当前消息识别搜索、下载和删除意图，只临时开放对应的漫画工具；未授权动作无法执行。
- 支持上一轮自然语言漫画搜索产生非空结果后的“下载第一个”等紧邻追问，隔轮或无结果不会继承授权。
- 保留 QQ `/jm` 命令和 Web 漫画搜索、下载按钮的直接调用路径，不改变现有 Owner 权限校验。
- 增加模糊表达、否定表达、能力询问、伪造工具调用和动作隔离测试。
- 完成后端 Pytest、Ruff、Pyright、前端 TypeScript 检查和 Vite 构建验证。

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
- 新增 [LangGraph 多知识库评测器](./scripts/langgraph_rag_eval.py)，记录真实工具调用、知识库选择、
  检索证据、最终回答、LLM Judge 评分和链路延迟。

#### 150 题真实评测结果

评测题集包含高频题 60 道、长尾题 60 道和模糊题 30 道，实际运行完整 LangGraph 检索链路。判分以
证据相关性和回答质量为准：`ALLinRAG` 与 `AI学习` 存在有效重合时，`ALLinRAG` 命中可以通过。

- 完成：150/150；运行错误：0。
- 检索相关性：3.793/4；检索充分性：3.793/4。
- 回答正确性：3.813/4；回答忠实性：3.820/4；引用质量：2.940/4。
- 端到端平均分：3.800/4；端到端通过率：94.67%。
- 证据判定选路失败：8/150（5.33%）；明确判定为有效重合命中：7/150（4.67%）。
- `ALLinRAG` 被检索：133/150 题；没有按知识库名称机械判错。
- 新停止策略覆盖的 21 道题中，实际检索均值 3.143 次，最大 4 次。

逐题 CSV、完整工具轨迹和 Judge 原始输出属于本地评测产物，不纳入公开仓库。

#### v0.3 验证与边界

- 当前验证：40 项 Pytest 通过、Ruff 通过、后端 Pyright 0 错误。
- 真实 DeepSeek V4 Flash 普通调用、流式调用、Tool Call 和 150 题 LangGraph 评测均已执行。
- 150 题的 Judge 使用当前模型自评；停止策略完整覆盖的是异常复测题和后续新增题，之前已正常完成的题
  保留原始基线结果，未全部重新运行。
- 当前评测结果用于本机个人 Agent 的工程基线，不代表公网多用户服务或独立人工金标结果。
- v0.3 起人格不再编译或全局分配；Web 和 QQ 会话独立选择人格，默认关闭。

### v0.2.0

本次发布前已通过 `scripts/check.ps1` 完整离线检查，包括依赖一致性、Python 编译、Ruff、Pyright、
34 项 Pytest、Vue TypeScript 检查和 Vite 构建。开发过程中还分别联调过主模型 SSE、原生 Tool
Calls、在线 Embedding、本地 BGE、FTS5/向量混合检索、在线 Reranker、长期记忆、JMComic 下载、
NapCat WebSocket、QQ 回复和文件发送。第三方服务会受账号、网络、接口和上游版本影响，发布结果不
代表这些外部链路在其他机器或未来始终可用。

自动测试不调用真实模型、JMComic 或 QQ；页面启动或 HTTP 200 也不能替代真实业务验证。

- 动态 Tools、Agent Skills、结构化人格、管理员管理、用户级记忆隔离和文件产物。
- NapCat WebSocket、QQ 回复、JMComic 下载及发送后显式清理流程。
