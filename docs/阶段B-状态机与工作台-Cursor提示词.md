# 阶段 B：合同状态机 + 工作台（给 Cursor 智能体）

## 角色与约束
你是本仓库的编码智能体。只做「阶段 B：合同生命周期状态机 + 工作台主路径」。
- **前置**：若阶段 A（`/api/health`、依赖与 README 安装段）尚未合并，不要在本任务里重做 A；若缺 health 也不要阻塞 B，可忽略前端健康点。
- 小步提交思维：先 schema/API，再 jobs 回写，再 workbench 页面与导航。
- 保持多租户：所有合同 API 必须按当前登录用户的 `user_id`/`owner` 过滤。
- 不要提交 git（除非用户明确要求）。
- 兼容现有页面：`review` / `ingest` / `kb` / `index` / `draft` / `inspect` 仍可直接打开；工作台是主入口，不是删掉旧页。

## 现状（必须基于此扩展）
- 表 `contract_files`（`store/storage.py`）字段大致为：`id, user_id, name, store_name, dir, size, note, folder_id, created_at`；**无 status**。
- 方法：`add_contract` / `list_contracts` / `get_contract` / `update_contract` / `delete_contract` 等；HTTP：`GET /api/files`、文件夹 API、`PATCH|DELETE /api/contracts/{id}`。
- 任务在 `agent/jobs.py`：内存任务；`create_job(kind, filename, target, owner=None, payload=None)`；与合同仅通过 **`filename`（常为 store_name）+ `owner`** 关联，**无 `contract_id`**；完成后无统一回调。
- 前端无共享 layout：各 HTML 复制 navbar；公共逻辑在 `static/main.js`。

## 目标

### B1. 状态机（数据层）
为每份合同增加生命周期字段（SQLite 与 MySQL 都要能工作）：
- `status`：枚举字符串，建议：
  - `uploaded` 已上传/已登记，未完成入库
  - `ingesting` 入库中
  - `ready` 已入库可审查
  - `reviewing` 审查中
  - `reviewed` 已出审查结果/报告
  - `archived` 已归档
  - `failed` 失败（可重试）
- `current_job_id`：可空，当前相关任务 id
- `last_error`：可空，失败原因短文本
- `status_updated_at`：可空或必填，状态变更时间

要求：
1. `storage` 层提供迁移：旧库启动时 `ALTER TABLE`/`PRAGMA` 检测列并补齐；已有行默认 `status='ready'`（若已在向量库/可审）或更保守统一 `ready`，并在注释/README 一句说明「旧数据默认 ready」。
2. 扩展 `update_contract`（或新增 `set_contract_status`）：可更新 `status/current_job_id/last_error/name/note/folder_id`；状态变更写 `status_updated_at`。
3. `list_contracts` / `GET /api/files`（或更明确的工作台 API）支持按 `status` 筛选；列表项带上新字段。
4. 定义合法流转（代码里集中一个函数或常量图），非法流转返回 400。建议允许：
   - → `ingesting` ← uploaded | failed(入库失败)
   - → `ready` ← ingesting 成功 | failed 手动恢复
   - → `reviewing` ← ready | reviewed（复审）| failed(审查失败)
   - → `reviewed` ← reviewing 成功
   - → `failed` ← ingesting/reviewing 失败
   - → `archived` ← ready | reviewed
   - `archived` → `ready` 或 `reviewed`（取消归档，保留原状态可用字段或直接回 ready）
5. 上传/登记新合同：初始 `uploaded`（若现有上传流程结束时已经入库成功，则直接 `ready`，以真实代码路径为准，但必须显式设状态）。

### B2. Jobs 与状态回写
在尽量少改 worker 签名的前提下：
1. `create_job` 增加可选 `contract_id`（写入 job 对象与 `payload`）；状态查询 API 能返回该字段。
2. 在 `agent/app.py` 里各 kind 的 job 完成/失败路径（review / ingest_analyze / ingest_commit / extract / inspect / report_summary 等）**统一**回写合同状态：
   - 开始：`ingesting` 或 `reviewing` + `current_job_id`
   - 成功：入库类 → `ready`；审查/报告类 → `reviewed`（若某种 job 不应改合同状态，在代码注释写明并跳过）
   - 失败/abort：`failed` + `last_error`，清空或保留 `current_job_id`（建议失败时清空 current_job_id，error 写入 last_error）
3. 用 `filename+owner` 反查合同的逻辑可保留，但 **优先用 contract_id**；找不到合同则打日志，不崩任务。

### B3. 工作台 API
在 `agent/app.py` 增加（名称可微调，但语义要有）：
- `GET /api/workbench/contracts`：当前用户合同列表；query：`status`、`q`（名称搜索）、分页可选
- `GET /api/workbench/contracts/{contract_id}`：详情 = 合同字段 + 最近任务摘要（若有）+ 可用操作列表（如可开始入库/审查/归档）
- `POST /api/workbench/contracts/{contract_id}/archive` 与 `…/unarchive`
- （可选）`POST …/retry`：从 `failed` 回到可重试状态（uploaded 或 ready，按 last 失败阶段判断，或简单回到 `uploaded`/`ready` 并清 last_error）

鉴权：与现有 `/api/files` 相同（登录用户）；禁止越权访问他人 `contract_id`。

### B4. 工作台前端
1. 新建 `static/workbench.html` + 必要 CSS（`style.css` 扩展，保持现有风格）。
2. `agent/app.py` 增加页面路由，例如 `/workbench` → `FileResponse`；**登录后默认落地页改为工作台**（`/` 仍可保留对话页，但登录成功跳转优先 `/workbench`；若改动登录跳转，检查 `login.html` / `main.js`）。
3. 工作台 UI 最低要求：
   - 合同表/卡片：名称、状态、更新时间、错误信息（若 failed）
   - 状态筛选 tabs 或下拉
   - 行内/详情内主操作：去入库、去审查、看对话/知识库、归档（链接到现有 `ingest.html` / `review.html` 等并带上文件名或 id query）
   - 空态文案：引导上传第一份合同（链到现有上传/入库入口）
4. **所有现有页面的 navbar** 增加「工作台」链接（与现有「复制 navbar」方式一致，或抽一小段由 `main.js` 注入导航项——若注入改动更小且不易漏页，优先注入）。
5. 进行中任务：若有 `current_job_id`，详情区显示「任务进行中」并链到现有 job 状态/SSE 用法（复用 `streamSSE` 等，不要重造一套）。

### B5. 文档
在 `README.md` 或 `WEB_PLAN.md` 简短增加「合同状态」说明与工作台入口；不要长篇重写。

## 验收模型约束（重要）
- **验收与联调不要依赖本地 Ollama 模型**（不要以 `ollama pull` / 本地 `qwen2.5:7b` 能跑作为必过条件）。
- **优先使用 DeepSeek 云端 API**：`.env` 中设 `LLM_PROVIDER=cloud`，并用已有的 `CLOUD_BASE_URL` / `CLOUD_API_KEY` / `CLOUD_MODEL`（如 `deepseek-chat` 或当前项目里的 deepseek 型号）。
- `/api/health` 仍应如实报告 `ollama` 是否可达，但 **Ollama 不可达不得阻碍你用云端完成功能验收**；文档里把「本地模型」写成可选加速/离线方案即可。
- 自动化或手动验证步骤中，凡调用 LLM 的路径（审查、入库分析、对话等）默认按云端配置验证；本地模型仅作可选说明，不写进必做验收清单。

## 明确不做
- 不做角色权限/审计表（阶段 D）
- 不做结构化风险落库与正式报告改造（阶段 C）
- 不做去 demo 账号（阶段 E）
- 不新增大改起草/巡检业务，只挂入口链接

## 验收标准
- 旧数据库启动不炸，旧合同有默认 status
- 走一遍：上传/入库开始 → 列表显示 ingesting → 成功 ready → 审查 reviewing → 成功 reviewed → 归档 archived
- 失败任务 → failed，且 last_error 有内容
- 用户只能看到自己的合同
- `/workbench` 可用，navbar 各页能点回工作台
- 旧 URL（`/review`、`/ingest` 等）仍可用

## 建议验证
1. `.env` 使用 `LLM_PROVIDER=cloud` + DeepSeek `CLOUD_*`，**不要**为验收启动/依赖本地 Ollama 模型
2. 用 SQLite 空库或现有库启动，看迁移日志/列是否存在
3. 浏览器登录 → 工作台列表 → 触发入库/审查（走云端 LLM），观察状态变化与刷新后是否保持
4. 用另一用户登录确认隔离
