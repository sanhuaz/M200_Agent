# M200 Agent v0.1.0

M200 Agent 是一个面向个人、本机单用户场景的 AI Agent 原型。它以 FastAPI、LangGraph 和
Vue 3 为核心，提供 Web 对话、三层记忆、文档混合检索、NapCat/OneBot v11 QQ 接入，以及
需要 Owner 确认的 JMComic 持久下载任务。

这是首个可运行原型版本，不是生产级多用户系统。项目默认绑定本机回环地址，不包含 Docker、
Redis、Celery、完整 RBAC、OCR、语音/视觉模型、生产日志平台或云部署配置。

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
- JMComic 搜索、十分钟 Owner 确认、单 Worker 下载、PDF 产物与 QQ 私聊发送。
- Vue 3 测试管理端：聊天、知识库、长期记忆、漫画任务和运行状态。

## 架构

```text
Web / QQ
   ↓
FastAPI API + OneBot WebSocket
   ↓
LangGraph Agent
   ├─ Chat Model / Function Calling
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

推荐 Python 3.13。项目不会在仓库中保存解释器路径；PowerShell 脚本按以下顺序选择 Python：

1. `PERSONAL_AGENT_PYTHON` 环境变量；
2. 当前 Conda 环境的 `CONDA_PREFIX\python.exe`；
3. `PATH` 中的 `python`。

Windows PowerShell 示例：

```powershell
$env:PERSONAL_AGENT_PYTHON = "C:\path\to\python.exe"
& $env:PERSONAL_AGENT_PYTHON -m pip install --no-user -r requirements.txt
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
MODEL_PROFILES_JSON=[{"alias":"default","model":"your-tool-capable-model","base_url":"https://provider.example/v1","api_key_env":"PERSONAL_AGENT_LLM_API_KEY","context_window":32768,"timeout_seconds":120}]
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
& $env:PERSONAL_AGENT_PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8000
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

- Web：http://127.0.0.1:5173
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
- `/help`、`/model list`、`/kb`、`/memory`、`/jm` 可用于查询。
- `/model use`、`/confirm`、下载和管理操作仅 Owner 可用。
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
- 下载必须由 Owner 在十分钟内确认。
- Worker 单并发，瞬时失败最多重试两次，默认只生成 PDF。
- QQ 仅向 Owner 私聊发送；超过阈值或发送失败时返回本地绝对路径。
- 用户应确保对下载内容拥有合法保存权利。

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

## v0.1.0 验证范围

开发环境已分别验证主模型 SSE、原生 Tool Calls、在线 Embedding、本地 BGE、FTS5/向量混合检索、
在线 Reranker、长期记忆写入与召回、JMComic 搜索/确认取消，以及 NapCat WebSocket 断线重连。
这些结果不代表所有第三方服务、账号和网络环境始终可用。

自动测试不调用真实模型、JMComic 或 QQ；页面启动或 HTTP 200 也不能替代真实业务验证。

## 安全与发布边界

公开仓库不包含：

- `.env` 和任何 API Key、Token、QQ 号；
- SQLite 数据库、Chroma 索引、上传文档和下载产物；
- 模型缓存、运行日志、真实测试结果和本机绝对路径；
- NapCat 本机配置。

应用默认仅监听 `127.0.0.1`。如需暴露到局域网或公网，应另行增加身份认证、权限控制、TLS、
限流和完整审计，不应直接复用当前 V1 配置。
