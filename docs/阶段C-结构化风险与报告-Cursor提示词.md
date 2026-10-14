# 阶段 C：结构化风险 + 可落地报告（给 Cursor 智能体）

## 角色与约束
你是本仓库编码智能体。只做阶段 C，不要做角色权限/审计（D）或去 demo 化（E）。
- 小步实现：先 DB + 落库，再 API，再审查页，再导出。
- 不要提交 git（除非用户明确要求）。
- **验收与联调使用云端 DeepSeek**：`.env` 中 `LLM_PROVIDER=cloud` + `CLOUD_*`；不要把本地 Ollama 模型作为必过条件。
- 多租户：findings 必须按合同归属用户隔离，禁止越权读写。
- 保持现有 `POST /api/analyze` + SSE 体验可用；在此之上**持久化**，不要改成只靠 sessionStorage。

## 现状（必须基于此扩展）
- 审查：`POST /api/analyze` → job kind `review` → `run_review_job`；维度分析在 `agent/contract_analyzer.py` · `analyze_dimension`（字段大致：`dimension` / `risk_level` / `evidence` / `opinion`）。
- 结果目前在 **`Job.results` 内存 + SSE**；前端 `review.html` 卡片展示；另有 `sessionStorage`。**无 DB findings。**
- `report_agent.summarize`：对 8 维 JSON 做文本摘要，进 job/SSE，**不导出文件**。
- `draft_export` 只服务起草下载，与审查报告无关。
- `contract_files` 已有状态机；审完会到 `reviewed`，但不存发现明细。

## 目标

### C1. Findings 数据模型（`store/storage.py` 等）
新建表（名可用 `review_findings`），建议字段：
- `id`, `contract_id`, `user_id`
- `dimension`（审查维度）
- `severity`（如 high/medium/low/info，与现有 risk_level 对齐并规范化）
- `title`（短标题；若模型只给 opinion，可截取生成）
- `detail`（意见/说明，对应 opinion）
- `quote`（原文依据，对应 evidence）
- `location`（可空 JSON 或文本：page/chunk_id/段号；现有 analyzer 若无位置，先可空，但字段要预留）
- `status`：`open` | `accepted` | `dismissed` | `fixed`（默认 `open`）
- `note`（处理备注，可空）
- `job_id`（来源审查任务，可空）
- `created_at`, `updated_at`

要求：
1. SQLite + MySQL 均可建表；启动迁移与现有 `_ensure_*` 风格一致。
2. CRUD：按 `contract_id` 列表/筛选 status；按 id 更新 status/note；删除合同时应级联或显式清理 findings。
3. **一次新的完整审查成功后**：可策略为「归档旧 open 为 superseded 或删除同 contract 旧 findings 再写入本次」——选一种写清并实现；避免无限重复堆叠且丢失历史时可接受「仅保留最近一次审查 + 可选 job_id」。

### C2. 审查落库
1. 扩展 `analyze_dimension`（或在 `run_review_job` 聚合后）产出可映射到 findings 的结构化字典；兼容旧前端字段名。
2. `run_review_job` 在各维完成/全部完成时写入 DB；失败不写半套或写明回滚策略。
3. job 的 SSE 仍推送结果，前端即时体验不变。
4. 合同状态继续走现有 `reviewing` → `reviewed`。

### C3. Findings API（`agent/app.py`）
需登录且校验合同归属，例如：
- `GET /api/contracts/{contract_id}/findings?status=`
- `PATCH /api/findings/{finding_id}`：更新 `status` / `note`
- `GET /api/contracts/{contract_id}/findings/summary`：各 severity/status 计数（供工作台/审查页）

### C4. 审查页产品化（`static/review.html`）
1. 审查结束后（或进入页时若已有 contract_id）：从 API 拉 findings，而不只依赖内存。
2. 「发现列表」：可筛选未处理（open）；点一条展示 quote/detail；若暂无原文高亮跳转，至少展示 quote 文本。
3. 每条可改 status（open/accepted/dismissed/fixed）+ 备注，调 PATCH。
4. 保留维度卡片流式体验亦可，但**权威数据以 DB findings 为准**。
5. 若 URL/选择器能带 `contract_id` 更好；否则用 store_name 反查合同再拉 findings。

### C5. 正式报告导出
1. 新增或扩展模块（推荐 `agent/report_export.py`，可复用 reportlab/python-docx）：输入 = DB findings（可只含 open+accepted 或全部，需在 UI 可选）+ 合同元信息，输出 **docx 与 pdf**。
2. API：如 `POST /api/contracts/{contract_id}/report/export?format=docx|pdf` → FileResponse 下载；内容基于结构化 findings，**不要**再整份只能靠 LLM 散文且不落库。
3. `report_agent.summarize` 可保留为「报告解读」Tab 的辅助，但导出必须以 findings 为准；若需封面摘要可先规则拼接，LLM 摘要可选。
4. 导出成功不强制改状态；合同已是 `reviewed` 即可。可选：把最近导出文件路径记 note（非必须）。

### C6. 工作台轻量联动（可选但推荐）
`workbench` 详情或列表显示「未处理风险数」（调 summary API）；链到 `/review` 并带上合同参数。

### C7. 文档
README 短节：findings 状态含义、如何导出报告；注明验收用云端模型。

## 明确不做
- 不做 RBAC/审计表（阶段 D）
- 不做去 demo、不改 health/工作台状态机（已完成）
- 不要求完美 PDF 排版；可读、含合同名/维度/风险/原文摘录/处理状态即可
- 不强制实现 PDF 内锚定翻页；location 预留即可

## 验收标准
1. 跑完一次审查（云端 LLM）后，刷新页面 findings 仍在。
2. 修改某条 status 后刷新保持。
3. 能下载 docx/pdf，内容含条目与 quote。
4. 另一用户无法读写该合同 findings。
5. Ollama 不必启动即可完成上述验收。

## 建议验证
```text
# LLM_PROVIDER=cloud + CLOUD_*
python app.py
# 浏览器：工作台选合同 → 审查 → 改状态 → 导出
# 或 curl 带登录 cookie/token 测 findings API（按项目现有鉴权方式）
```

## 回报
修改文件列表；表结构；API 列表；如何从 UI 走通验收路径。
