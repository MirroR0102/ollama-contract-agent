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

## 二、目录结构

```
ollama_contract_agent/
├── .env / .env.example      # Ollama 地址与模型配置
├── requirements.txt         # 依赖清单
├── config.py                # 全局配置（模型/向量库/审查维度）
├── embedding_client.py      # 本地嵌入模型封装（OllamaEmbeddings）
├── document_loader.py       # 文档加载与智能分块（txt/pdf）
├── vector_store.py          # Chroma 向量库：入库/去重/检索/统计/清空
├── contract_kb.py           # 知识库 RAG 问答（本地 ChatOllama）
├── contract_analyzer.py     # 合同 8 维度智能审查引擎
├── element_extractor.py     # 合同关键要素结构化抽取
├── tools.py                 # @tool 工具集（供 Agent 调用）
├── agent_run.py             # LangGraph 记忆 Agent 交互对话
├── main.py                  # 系统菜单入口（答辩演示用）
└── contracts/               # 演示合同文档（3 份样例，可自行替换）
```

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

## 四、快速运行

```bash
# 方式一：菜单式全流程（推荐答辩演示）
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
7. **收尾**：对比"直接问模型（幻觉）vs RAG（有据）"，讲可扩展方向（FastAPI/Web、批量 PDF、联网工具）

## 五、核心技术设计（答辩问答储备）

- **全程流式输出**：RAG 问答、合同审查、要素抽取、Agent 对话均基于 `llm.stream` 逐 token 生成（打字机效果），
  生成过程实时可见、无"等待假死"观感；Agent 对话用 `stream_mode="messages"` 实时展示"调用工具→工具返回→流式作答"全过程。
- **无幻觉保障**：审查/问答均强制"先检索原文片段，再让模型基于片段作答"，并把证据原文一并输出，可从机制上说明如何抑制幻觉。
- **定向检索防串库**：`contract_analyzer` 审查时按 `source` 元数据过滤向量库，保证只分析目标合同，不会混入其他合同内容。
- **多轮记忆**：`MemorySaver` + `thread_id` 实现会话级短期上下文记忆，隔离不同用户的对话。
- **可扩展架构**：模型全走 `.env` 配置；新增工具只需在 `tools.py` 加 `@tool` 函数并加入 `ALL_TOOLS`；后续可封装 FastAPI、接入 Web 界面。

## 六、校验自查（对照课程标准）
- [x] Ollama 本地运行，`qwen2.5:7b` + `bge-m3` 下载完成，无外网依赖
- [x] 支持 txt/pdf 加载、智能分块、入库本地 `chroma_db`
- [x] RAG 问答完全取自合同原文，无编造幻觉
- [x] Agent 多轮记忆 + 自动调用知识库/审查/抽取工具
- [x] 数据全部保存在本地，断网可运行
