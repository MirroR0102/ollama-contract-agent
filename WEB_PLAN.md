# Web 版开发计划（网页化改造）

> 目标：把本地化合同审查系统（Contract AI · Ollama 离线版）封装为**本地网页版**，
> 复用现有 Python 模块，FastAPI 提供接口 + SSE 流式，前端 4 个页面。
> 用途：课程答辩演示 + 求职简历（展示服务化封装 / AI 应用工程师能力）。

## 一、整体架构

```
浏览器(HTML/JS) --HTTP/SSE--> FastAPI(localhost:8000) --> 现有模块 --> Chroma 向量库
                                                              └--> Ollama(qwen2.5:7b + bge-m3)
```

现有模块：agent_run(Agent对话) / contract_kb(RAG问答) / contract_analyzer(合同审查)
         / element_extractor(要素抽取) / vector_store(入库检索)

## 二、目录结构（在 ollama_contract_agent 内新增）
```
├── (现有 10 个模块不动)
├── app.py                # FastAPI 主入口（路由 + SSE 流式）
├── uploads/              # 网页上传的合同（加入 .gitignore）
├── static/
│   ├── kb.html           # ① 知识库问答页（纯 RAG + 证据展示）
│   ├── index.html        # ② 智能体对话页（Agent + 工具 + 多轮记忆）【默认首页】
│   ├── review.html       # ③ 合同审查页（8 维度流式报告）
│   ├── ingest.html       # ④ 合同入库页（上传 + 入库）
│   ├── style.css
│   └── main.js           # SSE 流式读取等公共逻辑
└── requirements.txt      # + fastapi uvicorn python-multipart
```

## 三、接口设计
| 接口 | 方法 | 功能 | 返回 |
|---|---|---|---|
| /api/kb/query | POST | 知识库 RAG 问答（检索证据 + 流式回答） | SSE 流式 |
| /api/chat | POST | Agent 多轮对话（含工具调用事件） | SSE 流式 |
| /api/analyze | POST | 合同 8 维度审查 | SSE 流式 |
| /api/extract | POST | 要素抽取 | SSE 流式 |
| /api/ingest | POST | 上传 txt/pdf 自动入库 | JSON |
| /api/files | GET | 合同清单 | JSON |

## 四、关键技术决策
1. **SSE 流式**：FastAPI `StreamingResponse` 输出 SSE；前端用 `fetch` + `ReadableStream` 逐行解析（POST 不能用 EventSource）
2. **对话记忆**：复用 `MemorySaver` + `thread_id`，前端"新对话"= 新 thread_id
3. **模型并发**：FastAPI 同步接口 + 线程池即可（Ollama 串行处理）
4. **上传安全**：校验扩展名 txt/pdf，存 uploads/
5. **纯本地**：localhost:8000，模型走 Ollama，零云依赖

## 五、开发顺序（分 4 步，每步可验收）
1. **FastAPI 骨架 + 知识库问答页**（kb.html，先跑通 SSE 流式 RAG）
2. **智能体对话页**（index.html，SSE 流式 Agent + 工具过程展示）
3. **合同审查页**（review.html，复用流式审查）
4. **入库页**（ingest.html，上传 + 文件管理）→ 美化 + 联调 + 更新 GitHub/README

## 六、页面与演示对应（答辩层次）
1. 知识库问答页 → 讲 RAG 检索原理 + 无幻觉（证据展示）
2. Agent 对话页 → 讲工具调用 + 多轮记忆
3. 合同审查页 → 讲 AI 辅助业务（8 维度定向检索防幻觉）
4. 入库页 → 讲文档处理链路

## 七、启动方式（规划）
```powershell
cd '...\ollama_contract_agent'
D:\anaconda\python.exe -m pip install fastapi uvicorn python-multipart   # 首次
D:\anaconda\python.exe app.py   # 然后浏览器开 http://localhost:8000
```
