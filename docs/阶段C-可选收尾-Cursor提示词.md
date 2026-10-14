# 阶段 C 可选收尾（给 Cursor 智能体）

## 角色与约束
只做下列可选增强，不做阶段 D（角色/审计）。小步改动；勿提交 git（除非用户要求）。验收用 `LLM_PROVIDER=cloud` + DeepSeek，不依赖本地 Ollama。

## 任务 1：再审保留已处理结论（推荐做）
现状：新审查成功会 `replace_contract_findings` 整批覆盖，已 `dismissed`/`fixed`/`accepted` 会丢。

改法（选一种写清并实现，推荐 A）：
- **A（推荐）**：写入新 findings 前，按「维度 + quote 规范化（去空白/截断）」或「维度 + title」与旧条目匹配；若旧 status 为 `dismissed`/`fixed`/`accepted`，新条目继承该 status 与 note，并可选标 `note` 追加「复发仍维持原处理」；匹配不上的新开为 `open`。
- **B**：旧 findings 不删，改为 `status=superseded` + `job_id` 指向旧任务，新 job 写入新行；列表默认隐藏 superseded。

API/UI：发现列表默认仍看当前有效集；可保留「仅 open」筛选。

## 任务 2：location 尽量有值（尽力而为）
- 在 `contract_analyzer` / 落库映射处：若能从检索 chunk 拿到 `chunk_id`/页码/段落，写入 findings.`location`（JSON 字符串亦可）。
- 取不到则保持空，**禁止**为凑字段调用额外大模型。
- `review.html`：若有 location 则展示；有 chunk 引用时可链到已有定位能力（没有则只展示文本）。

## 任务 3：实机验收说明（文档一行即可）
README 审查节补一句手动路径：工作台 → 审查 → 改状态 → 导出 docx/pdf；注明云端验收。

## 明确不做
- 不改 RBAC/审计、不改 health/状态机主流程、不重做导出排版。

## 验收
1. 同一合同审两次：先前 dismissed 的同类项再次出现时仍为 dismissed（或 superseded 策略下旧状态可查）。
2. 至少在一种有检索上下文的路径上，部分 findings 的 location 非空（若数据实在无法取到，在回报里说明原因，不硬崩）。
3. 云端可完成审查与导出。

## 回报
改动文件、匹配策略、如何手动验证。
