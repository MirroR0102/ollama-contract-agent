# 阶段 A：环境与运维（给 Cursor 智能体）

## 角色与约束
你是本仓库的编码智能体。只做「阶段 A：环境与运维」，不要做阶段 B（合同状态机/工作台）或其它产品化需求。
- 尽量小步改动，保持现有 API 与前端行为兼容。
- 改完后用文字说明：改了哪些文件、如何本地验证。
- 不要提交 git（除非用户明确要求）。
- 不要把 `.env` 里的真实密钥写进文档或示例。

## 项目背景
本地化智能合同审查（Contract AI）：FastAPI 入口为根目录 `app.py`（薄壳），真实路由在 `agent/app.py`；配置在 `core/config.py`；Ollama 在 `core/ollama_conn.py`；双引擎在 `core/llm_provider.py`。

## 目标（全部完成才算阶段 A 结束）
1. **依赖对齐**：`requirements.txt` 补上代码已用但未声明的依赖（至少包括 `draft_export` 使用的 `reportlab`；再扫一遍 import，缺什么补什么，版本约束风格与现有文件一致）。
2. **开发默认可开箱**：`.env.example` 将开发默认改为 **SQLite**（`DB_ENGINE=sqlite`，保留 `DB_PATH=./contract_ai.db`），MySQL 段保留并注释说明「生产可选」；键名与现有一致：`OLLAMA_HOST`、`LLM_MODEL`、`EMBED_MODEL`、`LLM_PROVIDER`、`CLOUD_*`、`CHROMA_PATH`、`CONTRACTS_DIR`、`CHUNK_*`、`DEMO_*` 等。可补充注释说明 `JOB_ABANDON_TIMEOUT`（若代码支持）。**不要改用户现有 `.env` 里的密码/密钥**；若必须动 `.env`，只加缺失的安全默认项并在说明里写出。
3. **健康检查 API**：在 `agent/app.py` 新增 `GET /api/health`（或同时提供 `GET /health` 指向同一实现），**无需登录**，返回 JSON，大致字段：
   - `ok`：整体是否可提供基本服务（DB 可用即可为 true；模型不可用时仍可 true，但要用子字段标明）
   - `db`：`{ "ok": bool, "engine": "sqlite"|"mysql", "detail": "..." }`
   - `ollama`：`{ "ok": bool, "host": "...", "detail": "..." }`（对 `OLLAMA_HOST` 探活，超时要短，如 1–2s）
   - `models`：`{ "llm": "...", "embed": "...", "llm_present": bool|null, "embed_present": bool|null }`（若无法列模型，present 可为 null 并在 detail 说明）
   - `llm_provider`：当前 `local`/`cloud` 及云端是否配置（可复用 `cloud_status()`，不要打印 API Key）
   - `chroma`：路径是否可写/可访问
4. **实现探活复用**：优先在 `core/ollama_conn.py` / `core/llm_provider.py` / `store/bootstrap.py` 增加小函数供 health 调用，避免在路由里堆原始 requests；失败要捕获，**禁止**因 Ollama 挂了导致 health 接口 500。
5. **启动可观测**：`python app.py` 或 `bootstrap.init_system()` 时打印简短自检摘要（DB/Ollama/provider），失败用警告而不是直接崩溃（除非 DB 完全不可用——可按现有 bootstrap 行为，若现有会崩则保持一致并在 README 写明）。
6. **前端状态可见（轻量）**：在 `static/main.js` 的公共导航/用户条初始化里，请求 `/api/health`，用小圆点或简短文案显示 DB/Ollama 异常（绿/黄/红即可）。**所有带 navbar 的页面**都应能看到（现有页面是复制 navbar，改 `main.js` 的 `initNav`/`initPage`/`initUserBar` 一类入口，避免只改单一 HTML）。不要大改 UI 风格。
7. **文档**：更新 `README.md`「§三 环境准备」「§四 快速运行」：
   - 写清：复制 `.env.example` → `.env`、默认 SQLite、如何改回 MySQL、`pip install -r requirements.txt`、`python app.py` → `http://localhost:8000`
   - **推荐验收路径写云端**：`LLM_PROVIDER=cloud` + DeepSeek（`CLOUD_*`）；本地 Ollama / `ollama pull` 标为**可选**离线方案，不要写成唯一必做步骤
   - 写明 `/api/health` 用途
   - 把明显过时描述往「当前能力」靠拢时 **只改与安装/运行相关的过时句**；不要在本阶段重写整本 README 功能广告（起草/巡检/10 工具的完整重写可另做，但若快速开始里仍写错依赖/步骤则必须改对）

## 验收模型约束（重要）
- **验收与联调不要依赖本地 Ollama 模型**（不要以 `ollama pull` / 本地 `qwen2.5:7b` 能跑作为必过条件）。
- **优先使用 DeepSeek 云端 API**：`.env` 中设 `LLM_PROVIDER=cloud`，并用已有的 `CLOUD_BASE_URL` / `CLOUD_API_KEY` / `CLOUD_MODEL`（如 `deepseek-chat` 或当前项目里的 deepseek 型号）。
- `/api/health` 仍应如实报告 `ollama` 是否可达，但 **Ollama 不可达不得阻碍你用云端完成功能验收**；文档里把「本地模型」写成可选加速/离线方案即可。
- 自动化或手动验证步骤中，凡调用 LLM 的路径（审查、入库分析、对话等）默认按云端配置验证；本地模型仅作可选说明，不写进必做验收清单。

## 明确不做
- 不改合同表结构、不加 workbench、不改 jobs 与合同状态流转（那是阶段 B）
- 不做 docker-compose（可在 README 提一句「后续可选」，不要实现）
- 不删 demo 账号逻辑（去 demo 化是更后阶段）

## 验收标准
- 新克隆后：按更新后的 README，在 **SQLite + 云端 DeepSeek** 配置下能理解如何启动并验收；Ollama 仅作可选说明
- `GET /api/health` 在 Ollama 关闭时仍返回 200 JSON，且 `ollama.ok=false`
- `requirements.txt` 安装后 `draft_export` 不再因缺 reportlab 导入失败（若需可写一句验证方法）
- 登录后页面能看到模型/Ollama 异常提示（Ollama 挂起时）

## 建议验证命令
```text
# .env 验收配置示例（不要把真实 Key 写进仓库文件）：
# LLM_PROVIDER=cloud
# CLOUD_BASE_URL=https://api.deepseek.com 或带 /v1 的兼容地址（以代码实际要求为准）
# CLOUD_API_KEY=***
# CLOUD_MODEL=deepseek-chat  # 或项目正在用的 deepseek 型号
# DB_ENGINE=sqlite
pip install -r requirements.txt
python -c "import reportlab; print('reportlab ok')"
python app.py
# 另开终端：
curl -s http://127.0.0.1:8000/api/health
# 确认 JSON 里 llm_provider 为 cloud；Ollama 可为 ok=false，只要云端可用即可继续验收
```
