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
├── .env / .env.example       # Ollama / MySQL 配置（.env 含密码不入库）
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

## 三、环境准备（模型导入指引）

### 1. 安装 Ollama
官网 https://ollama.com/ 下载安装包，完成后**重启终端**，确认服务运行在 `http://localhost:11434`。

### 2. 下载本项目需要的 2 个模型
在终端执行（本项目所有配置已指向这两个模型，无需改代码）：

```bash
# ① 大语言模型：负责问答 / 审查 / 抽取 / Agent 推理
ollama pull qwen2.5:7b

# ② 向量嵌入模型：负责合同文档向量化检索
ollama pull bge-m3
```

> 设备性能有限可换小模型：`ollama pull qwen2.5:3b`（把 `.env` 里 `LLM_MODEL` 改掉即可）；
> 嵌入模型也可用 `bge-small` / `nomic-embed-text`（改 `.env` 的 `EMBED_MODEL`）。
> 模型名与配置不一致时会报错——改模型只需改 `.env`，端口默认 11434 不用动。

### 3. 验证模型可用
```bash
ollama list          # 应能看到 qwen2.5:7b 与 bge-m3
ollama run qwen2.5:7b   # 输入"你好"，正常回复即成功，输入 /bye 退出
```

### 4. 安装 Python 依赖
```bash
pip install -r requirements.txt
```

### 5.（可选）配置云端大模型（联网模式）
> 不配置也能用——默认全程本地 Ollama 离线推理；配置后可在网页端一键切换，获得更强的生成质量。

在 `.env` 中填入任意 **OpenAI 兼容** 服务的配置（以 DeepSeek 为例）：

```bash
CLOUD_BASE_URL=https://api.deepseek.com   # 兼容：通义/Kimi/硅基流动/OpenAI 等
CLOUD_API_KEY=sk-...                      # 留空则只能由用户在网页端填个人 Key
CLOUD_MODEL=deepseek-flash                # 按服务商文档填写模型名
```

* 网页端**右上角「模型设置」**可随时在 本地 ⇄ 云端 之间切换（按账号独立保存，重启后仍生效）；
* 未在 `.env` 配置时，用户也可在弹窗中填写**个人 Key**（仅存本机数据库、按账号隔离）；
* ⚠️ 联网模式下，检索到的合同片段会发送至云端服务商；**敏感合同请继续使用本地模式**。

> 嵌入（embedding）始终使用本地 bge-m3，不走云端，原因见「五、核心技术设计」。

## 四、快速运行

```bash
# 方式零：网页版（推荐答辩演示，浏览器访问 http://localhost:8000）
python app.py
# 页面：/login 登录注册（默认演示账号 demo/demo123）→ /kb 知识库问答  / 智能体对话  /review 合同审查  /ingest 合同入库
# 右上角「模型设置」可切换 本地/云端 模型引擎（云端需 .env 配置 CLOUD_API_KEY 或由用户填个人 Key）
# 首次启动自动建库并预置演示账号；注册新账号后上传的合同自动归属该账号

# 方式一：菜单式全流程（命令行演示）
python main.py

# 方式二：直接进入 Agent 多轮对话
python agent_run.py

# 方式三：单模块自检（需先入库）
python embedding_client.py     # 检查嵌入模型连通
python document_loader.py      # 检查文档分块效果
python vector_store.py         # 查看向量库状态
python contract_kb.py          # 测试知识库问答
python contract_analyzer.py    # 测试合同审查
python element_extractor.py    # 测试要素抽取
```

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
