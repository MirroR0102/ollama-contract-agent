# 阶段 D 可选收尾（给 Cursor 智能体）

## 约束
只做下列收尾，不做阶段 E。勿提交 git（除非用户要求）。云端验收，不依赖本地 Ollama。

## 任务
1. **统一缓存版本**：所有 HTML 中 `style.css?v=` 与 `main.js?v=` 改为同一新版本号（高于现有最大值）。含 `admin.html` 与各业务页。
2. **`WEB_PLAN.md`**：补一句三角色（admin/legal/viewer）与审计/管理页 `/admin`，与 README 对齐即可，勿长文重写。
3. **（可选文档）** README 加一行：如何用 admin 创建/改角色后，用 viewer 验证写接口 403。

## 明确不做
不改权限矩阵、不加新角色、不做去 demo。

## 验收
全站 css/js 的 `?v=` 一致；WEB_PLAN 能搜到角色或审计。
