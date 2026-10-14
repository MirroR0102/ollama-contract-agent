# 📑 本地化智能合同审查系统（Contract AI · Ollama 离线版）

> 基于 **LangChain 1.0 + LangGraph + Chroma + Ollama** 的企业级合同审查 Agent。
> 全程**本地离线推理**，不调用任何云端 API，满足数据安全/私有化部署要求。
> 课程项目《综合项目之企业级本地知识库模型搭建》的进阶实践：在标准本地知识库 RAG 基础上，
> 增加 **合同多维度智能审查、关键要素结构化抽取、带记忆的多工具 Agent**，形成完整业务闭环。

---

## 一、功能总览（答辩讲解主线）

| 功能 | 说明 | 对应课程知识点 |
|---|---|---|
| ① 合同文档导入 | 批量扫描 txt/pdf，智能分块入库 Chroma（内容去重、增量更新） | RAG 文本转换 / Chunk 处理 / 向量存储 |
| ② 知识库 RAG 问答 | 基于合同原文作答，**附引用出处、禁止幻觉** | Retrieval / 相似度检索 / LLM 封装 |
| ③ 合同智能审查 ⭐ | 按 8 个风险维度逐项"定向检索原文 + 分析"，输出风险等级/依据/修改建议 | Agent 工具 / 定向检索 / 提示词工程 |
| ④ 关键要素抽取 ⭐ | 结构化提取合同类型/当事人/金额/期限/日期/管辖等 | 结构化输出 / 提示词工程 |
| ⑤ 记忆 Agent ⭐ | LangGraph 多轮对话记忆，自动决策调用 4 个工具 | Agent / LangGraph / 短期上下文记忆 |
| ⑥ 向量库管理 | 状态统计 / 清空 | Chroma 管理 |
| ⑦ 网页版 ⭐ | FastAPI 封装全部能力 + SSE 流式，浏览器四页面交互 | 服务化封装 / 流式接口 |
| ⑧ 用户系统 ⭐ | 注册/登录 + MySQL 合同库归属 + Chroma owner 多租户隔离，各账号合同库完全独立 | 数据库设计 / 认证 / 权限隔离 |
| ⑨ 双模型引擎 ⭐ | 本地 Ollama（离线）⇄ 云端 OpenAI 兼容 API（DeepSeek 等）网页端一键切换；嵌入固定本地 bge-m3 | 大模型封装 / 可切换架构 |

## 二、目录结构（分层：core 底层 → store 数据 → agent 业务/服务）

```
ollama_contract_agent/
├── app.py / main.py          # 入口薄壳：python app.py(网页) / python main.py(命令行)
├── .env / .env.example       # 环境变量（开发默认 SQLite；.env 含密钥不入库）
├── requirements.txt          # 依赖清单
├── core/                     # ① 底层：配置与大模型/向量连接
│   ├── config.py             #   全局配置（本地/云端模型/向量库/审查维度/数据库）
│   ├── ollama_conn.py        #   本地 Ollama 连接管理 + 失效自动重建（自愈）
│   ├── llm_provider.py       #   双模型引擎网关（本地 Ollama ⇄ 云端 OpenAI 兼容 API）
│   └── embedding_client.py   #   本地嵌入模型封装（OllamaEmbeddings）
├── store/                    # ② 数据存取层
│   ├── storage.py            #   存储双后端：MySQL / SQLite（用户/文件夹/合同归属）
│   ├── auth.py               #   用户认证：PBKDF2 密码哈希 + token 会话
│   ├── bootstrap.py          #   启动初始化：demo 账号、演示合同归户、暂存认领
│   ├── session_context.py    #   会话 thread → 用户 / 上下文合同范围
│   ├── document_loader.py    #   txt/pdf 加载与中文合同智能分块
│   ├── docparse.py           #   多格式解析：txt/pdf/docx/图片OCR/zip
│   └── vector_store.py       #   Chroma 向量库：按 owner+contract_id 域隔离/检索/删除
├── agent/                    # ③ 业务与智能体层
│   ├── contract_kb.py        #   知识库 RAG 问答（无幻觉）
│   ├── contract_analyzer.py  #   合同 8 维度智能审查引擎
│   ├── element_extractor.py  #   合同关键要素结构化抽取
│   ├── intake_agent.py       #   文档接入智能体（多格式识别 + 压缩包切分）
│   ├── tools.py              #   @tool 工具集（供 Agent 调用）
│   ├── agent_run.py          #   LangGraph 记忆 Agent（对话智能体）
│   ├── jobs.py               #   后台任务引擎（SSE 进度/暂停/恢复）
│   ├── app.py                #   FastAPI 网页服务（全部 API）
│   └── main.py               #   命令行演示菜单（CLI）
├── static/                   # 网页前端（登录 + kb/对话/审查/入库 五页面）
├── uploads/                  # 网页上传的合同 + 暂存区（不入库 git）
├── contracts/                # 演示合同文档（3 份样例）
├── WEB_PLAN.md               # 网页版开发方案
└── chroma_db/                # 本地向量库（运行时生成，不入库 git）
```

> 包内模块使用绝对导入（`from core.config import ...` / `from store.x import ...` /
> `from agent.x import ...`）；请在项目根目录运行入口脚本（`./chroma_db`、`./contracts`
> 为相对当前工作目录路径）。

## 三、环境准备

在项目根目录操作。开发默认 **SQLite**，不需要单独安装数据库。生成类功能的推荐验收路径是 **云端 DeepSeek**；本地 Ollama 是可选离线方案。

### 1. 安装 Python 依赖
```bash
pip install -r requirements.txt
```
起草导出 PDF 依赖 `reportlab`。装完可确认：
```bash
python -c "import reportlab; print('reportlab ok')"
```

### 2. 准备环境变量
```bash
# Windows 可把 copy 换成：copy .env.example .env
cp .env.example .env
```
`.env.example` 里 `DB_ENGINE=sqlite`，数据库文件为 `DB_PATH=./contract_ai.db`。不要把真实密钥写进仓库。

**推荐验收配置（云端 DeepSeek）**，在 `.env` 中设置为：

```bash
LLM_PROVIDER=cloud
CLOUD_BASE_URL=https://api.deepseek.com/v1
CLOUD_API_KEY=sk-...                 # 到 https://platform.deepseek.com 创建，勿提交到 git
CLOUD_MODEL=deepseek-chat            # 或当前账号可用的 DeepSeek 模型名
DB_ENGINE=sqlite
```

* 网页端**右上角「模型设置」**可随时在 本地 ⇄ 云端 之间切换（按账号独立保存）；
* `.env` 未填 `CLOUD_API_KEY` 时，用户也可在弹窗中填写**个人 Key**（只存在本机数据库）；
* ⚠️ 联网模式下，检索到的合同片段会发送至云端服务商。

**改回 MySQL（生产可选）**：安装 MySQL 8.0+ 后，把 `DB_ENGINE` 改为 `mysql`，并填写 `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME`。数据库完全不可用时，进程会按现有启动逻辑失败退出；Ollama 不可达只打印警告，不阻止启动。

### 3. 启动
```bash
python app.py
```
浏览器打开 http://localhost:8000 。首次启动会建库并预置演示账号 `demo` / `demo123`。

无需登录即可查看环境：

```bash
curl -s http://127.0.0.1:8000/api/health
```

`GET /api/health`（同等路径 `GET /health`）返回数据库、Ollama、配置的模型是否在本机、当前引擎是 `local` 还是 `cloud`、以及 Chroma 目录是否可写。Ollama 没开时接口仍是 200，`ollama.ok` 为 `false`，`ok` 只要数据库可用就为 `true`。登录后的导航栏会用绿/黄/红提示数据库和 Ollama 状态。

### 4.（可选）本地 Ollama 离线方案
需要离线推理，或要做合同入库/检索时再安装。嵌入模型固定走本地，不走云端（原因见「五、核心技术设计」）。不装 Ollama 也可以按上一节用云端完成生成类验收。

1. 安装 Ollama：https://ollama.com/ ，完成后重启终端，默认地址 `http://localhost:11434`（`.env` 的 `OLLAMA_HOST`）。
2. 需要本地对话模型时：`ollama pull qwen2.5:7b`（机器较小时可改为 `qwen2.5:3b`，并同步改 `LLM_MODEL`）。
3. 需要入库/检索时：`ollama pull bge-m3`（备选 `bge-small` / `nomic-embed-text`，改 `EMBED_MODEL`）。换嵌入模型后要重建 `chroma_db`。
4. 自检：`ollama list`。把 `LLM_PROVIDER` 改回 `local` 即走本地生成。

## 四、快速运行

```bash
# 方式零：网页版（推荐，浏览器访问 http://localhost:8000）
python app.py
# 页面：/login 登录注册（默认演示账号 demo/demo123）→ /kb 知识库问答  / 智能体对话  /review 合同审查  /ingest 合同入库
# 右上角「模型设置」可切换 本地/云端；验收请使用 LLM_PROVIDER=cloud
# 健康检查：http://localhost:8000/api/health
# 首次启动自动建库并预置演示账号；注册新账号后上传的合同自动归属该账号

# 方式一：菜单式全流程（命令行演示；调用大模型时同样跟随 LLM_PROVIDER）
python main.py

# 方式二：直接进入 Agent 多轮对话（在项目根目录执行）
python -m agent.agent_run

# 方式三：单模块自检（在项目根目录执行；嵌入/入库相关项需要可选的本地 Ollama）
python -m core.embedding_client      # 检查嵌入模型连通
python -m store.document_loader      # 检查文档分块效果
python -m store.vector_store         # 查看向量库状态
python -m agent.contract_kb          # 测试知识库问答
python -m agent.contract_analyzer    # 测试合同审查
python -m agent.element_extractor    # 测试要素抽取
```

后续若要容器化，可再加 docker-compose；本阶段不提供编排文件。

### 合同状态与工作台
登录后进入 `/workbench`。合同状态为 `uploaded`（待入库）→ `ingesting` → `ready`（可审查）→ `reviewing` → `reviewed`，失败是 `failed`，`ready` / `reviewed` 可归档为 `archived`。升级前已经在库里的合同默认 `ready`。审查、抽取和报告解读会回写对应合同；入库分析（还没有合同记录）和全库巡检不改某一份合同的状态。`/review`、`/ingest` 等旧页面仍可直接打开。

### 审查发现与报告导出
一次审查全部完成后，结果写入 `review_findings`，只保留该合同最近一次审查。刷新审查页后仍可在「发现列表」查看原文摘录和意见。处理状态为 `open`（未处理）、`accepted`（已接受）、`dismissed`（已忽略）、`fixed`（已修改）。同一维度、同一段原文再次审到时，已接受、已忽略、已修改的结论会保留。导出 Word / PDF 以这些记录为准，不重新生成散文。手动验收：工作台打开合同 → 去审查 → 在发现列表改状态 → 导出 docx 或 pdf。功能验收使用云端模型（`.env` 里 `LLM_PROVIDER=cloud`）；检索嵌入仍走本地模型。

### 角色与审计
三个角色：`admin` 管用户、角色、审计和模型设置，也能做审查入库；`legal` 做上传、入库、审查、改发现、导出和归档，不能进管理页；`viewer` 只能看工作台、发现和已有报告，不能开始审查、入库、改状态或删合同。新注册用户默认 `legal`。库里还没有用户时，第一个注册的人是 `admin`。启动时如果还没有任何管理员，会把 `BOOTSTRAP_ADMIN_USERNAME` 指定的用户升为 admin；该项留空则提升最早注册的用户（通常是 demo）。演示账号仍在，角色可在 `/admin` 改回去。审计记登录成败、入库、开始审查、导出、发现状态、归档、删合同、改角色和模型设置（只记是否更新了密钥，不记密钥和密码）。
验证：用 admin 在 `/admin` 把某个账号改成 viewer，再用该账号调开始审查、改发现或删合同，接口应返回 403。

### 演示动线建议（覆盖 20 分钟答辩）
1. **开场**：项目背景（企业合同数据敏感不可上云 → 本地私有化 RAG/Agent）
2. **功能 1 入库**：批量导入 `contracts/` 3 份合同，展示分块与去重逻辑
3. **功能 2 RAG 问答**：问"采购合同的付款节点？"展示检索证据 + 无幻觉回答
4. **功能 3 合同审查**：对《采购合同》审查 8 维度，重点讲**定向检索防幻觉**设计，展示查出的高风险项（如逾期付款违约金 0.5%/日、争议管辖在乙方所在地、责任上限=合同总价等）
5. **功能 4 要素抽取**：展示结构化 JSON 输出
6. **功能 5 Agent**：演示多轮记忆（先问 A 合同、再追问），展示 Agent 自主选工具过程
7. **收尾**：对比“直接问模型（幻觉）vs RAG（有据）”，展示右上角「模型设置」一键切换云端大模型（质量对比），讲可扩展方向（FastAPI/Web、批量 PDF、联网工具）

## 五、核心技术设计（答辩问答储备）

- **全程流式输出**：RAG 问答、合同审查、要素抽取、Agent 对话均基于 `llm.stream` 逐 token 生成（打字机效果），
  生成过程实时可见、无"等待假死"观感；Agent 对话用 `stream_mode="messages"` 实时展示"调用工具→工具返回→流式作答"全过程。
- **无幻觉保障**：审查/问答均强制"先检索原文片段，再让模型基于片段作答"，并把证据原文一并输出，可从机制上说明如何抑制幻觉。
- **定向检索防串库**：`contract_analyzer` 审查时按 `source` 元数据过滤向量库，保证只分析目标合同，不会混入其他合同内容。
- **多轮记忆**：`MemorySaver` + `thread_id` 实现会话级短期上下文记忆，隔离不同用户的对话。
- **多租户用户隔离**：注册/登录（token 会话）+ 数据库记录每份合同归属 + Chroma 切片按 owner 过滤；
  检索/审查/抽取/上传全部限定当前账号，不同账号的合同库**完全不互通**（含越权 404 防护）。
- **双模型引擎（本地 ⇄ 云端）**：`core/llm_provider.py` 统一网关，业务代码无感——网页端右上角
  可按用户切换 本地 Ollama / 云端 OpenAI 兼容 API（DeepSeek 等），偏好持久化在 users 表；
  每个请求/后台任务按登录用户注入偏好（contextvars），云端客户端懒加载、配置变化自动重建；
  云端错误（401/402/429/超时）统一映射为可操作中文提示。
- **为什么嵌入保持本地 bge-m3**：① bge-m3 是开源顶尖多语言嵌入模型，中文合同检索已足够；
  ② 切换嵌入模型=向量空间改变，必须全量重建向量库，且合同全文将上传云端；③ DeepSeek 等生成平台
  并不提供嵌入接口。因此“生成质量”可用云端 LLM 解决，而“数据不出域”由本地嵌入 + 本地模式兜底。
- **可扩展架构**：模型全走 `.env` 配置；新增工具只需在 `tools.py` 加 `@tool` 函数并加入 `ALL_TOOLS`；后续可封装 FastAPI、接入 Web 界面。

## 六、校验自查（对照课程标准）
- [x] Ollama 本地运行，`qwen2.5:7b` + `bge-m3` 下载完成，本地模式无外网依赖
- [x] 支持 txt/pdf 加载、智能分块、入库本地 `chroma_db`
- [x] RAG 问答完全取自合同原文，无编造幻觉
- [x] Agent 多轮记忆 + 自动调用知识库/审查/抽取工具
- [x] 可选云端大模型（OpenAI 兼容，如 DeepSeek）：网页端一键切换，按用户生效
- [x] 数据全部保存在本地，断网可运行
