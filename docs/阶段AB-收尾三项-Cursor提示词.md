# 阶段 A/B 收尾三项（给 Cursor 智能体）

## 角色与约束
你是本仓库编码智能体。只做下列三项收尾，不做阶段 C（结构化风险/报告）或其它产品化大改。
- 小步改动；不要提交 git（除非用户明确要求）。
- **不要**把真实 `CLOUD_API_KEY` 或数据库密码写进任何会进仓库的文件；改 `.env` 时只改键值策略，说明里用 `***`。
- 验收优先云端：`LLM_PROVIDER=cloud` + DeepSeek `CLOUD_*`；不要把本地 Ollama 当必过。

## 任务 1：实机联调配置（`.env`）
1. 打开项目根目录 `.env`（勿提交到 git）。
2. 将 `LLM_PROVIDER` 改为 `cloud`（若已是 cloud 则跳过）。
3. 确认 `CLOUD_BASE_URL` / `CLOUD_API_KEY` / `CLOUD_MODEL` 已填写且与当前代码读取方式一致（以 `core/config.py` / `llm_provider` 为准；Base URL 是否带 `/v1` 跟现有实现，不要想当然改挂）。
4. **不要改**用户的 Key 内容；若缺键则按 `.env.example` 补键名并留空让用户填。
5. `DB_ENGINE`：本任务**不强制**改 MySQL→SQLite（避免动生产数据）。若只为本地轻量验收且用户同意，可另开说明；默认保持现状，只保证云端 LLM 生效。
6. 改完后用文字回报：改了哪些键（不要打印密钥）。

## 任务 2：更新 `WEB_PLAN.md`
在 `WEB_PLAN.md` 适当位置（总览或页面列表处）补上与现状一致的短说明，至少包括：
- 主入口：工作台 `/workbench`（合同状态列表与主路径）
- 合同状态机字段与状态枚举（与 README / `store/contract_status.py` 一致：`uploaded|ingesting|ready|reviewing|reviewed|archived|failed`）
- 说明旧「四页面」表述已过时：现有 login / workbench / kb / 对话 / review / ingest / draft / inspect 等
保持文档风格，不要整篇重写成长文。

## 任务 3：前端缓存版本 + health 文案（云端友好）
1. **统一**所有 HTML 里引用的 `main.js?v=` 为同一个新版本号（例如都改为比现有最大值 +1），避免有的页仍缓存旧 JS（看不到工作台注入或健康点）。
2. 优化 `static/main.js` 里 `initServiceHealth`（或等价逻辑）的展示文案：
   - 当 `ollama.ok === false` 且云端已配置/可用（health JSON 里 `llm_provider` / `cloud` 一类字段显示已配置或 ok）时，不要只吓人地写「Ollama 离线」；改为类似：**「本地离线（云端可用）」**，圆点可用黄而非红（若 DB 也挂再红）。
   - 当云端也未配置且 Ollama 挂：保持明确告警。
   - DB 失败仍应优先标红/阻断感强于「仅本地离线」。
3. 若 health API 字段名与前端不一致，以实际 `/api/health` JSON 为准适配，不要臆造字段。

## 明确不做
- 不改状态机流转、不加新 API、不做结构化风险落库
- 不删 demo 账号、不做 docker-compose

## 验收
1. `.env` 中 `LLM_PROVIDER=cloud`（密钥不出现在 diff/文档）
2. `WEB_PLAN.md` 能搜到 workbench / 状态机简述，不再把产品说成只有 4 个前端页
3. 全站 `main.js?v=` 一致；断掉 Ollama、云端可用时，顶栏健康提示体现「本地离线（云端可用）」
4. `curl -s http://127.0.0.1:8000/api/health` 仍 200（若需可临时启动 `python app.py`）

## 回报
列出修改文件；说明如何手动点开一页验证健康文案与导航「工作台」。
