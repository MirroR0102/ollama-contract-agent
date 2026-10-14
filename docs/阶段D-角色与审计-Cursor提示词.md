# 阶段 D：角色权限 + 操作审计（给 Cursor 智能体）

## 角色与约束
只做阶段 D，不做阶段 E（去 demo 化）除非顺手改文案一句。
- 小步：先 role 字段与鉴权，再审计表与埋点，最后管理页。
- 勿提交 git（除非用户要求）。
- 验收用云端 DeepSeek（`LLM_PROVIDER=cloud`），不依赖本地 Ollama。
- 保持现有多租户：`user_id` / owner 隔离；**admin 默认仍只能管本系统用户，不要做成可看所有人合同的上帝视角**（除非加显式开关且默认关）。


## 现状（必须基于此扩展）
- `users` 现有字段：`id, username, password_hash, display_name, llm_provider, cloud_api_key, cloud_model, created_at`；**无 role**；schema_version 现为 5。
- 认证：`store/auth.py`（register/login/sessions）；`agent/app.py`：`POST /api/auth/register|login|logout`，`GET /api/me` 目前只回 username/display_name。
- `current_user`：Bearer / `?token=` / `x-token` → `user_by_token`。
- **无** RBAC、无 audit 表、无 `/api/admin*`、无管理页；隔离靠 user_id/owner。
- `list_users` 若已在 storage，可复用并挂到 admin API。

## 优先挂审计的现有写操作
登录失败（及可选成功）、`POST /api/ingest/commit`、开始审查相关（如 `POST /api/analyze` / review 启动）、`PATCH /api/findings/{id}`、`POST .../report/export`、`DELETE` 合同/文件夹、workbench archive/unarchive、`POST /api/settings/model`（**detail 禁止明文 Key**）、以及本阶段新增的改角色。


## 目标角色（先三档）
| role | 能力 |
|------|------|
| `admin` | 用户列表、改角色、看审计；模型/系统设置若已有则仅 admin；也可做 legal 能做的业务 |
| `legal` | 上传/入库/审查/改 findings/导出报告/归档等写操作 + 只读 |
| `viewer` | 只读：工作台列表/详情、知识库与报告查看、findings 只读；**不能**开始审查/入库/改 findings/删合同/改设置 |

注册默认：`legal`（若已有大量用户，迁移默认 `legal`）。第一个用户或 `.env` 指定 `BOOTSTRAP_ADMIN_USERNAME` 可升为 admin（实现一种即可，写进 README）。

## D1. 数据层
1. `users` 表加 `role`（TEXT/ENUM），迁移旧用户 → `legal`。
2. `current_user` / session 载荷带上 `role`。
3. 新建 `audit_logs`：`id, user_id, username, action, target_type, target_id, detail(json/text), ip(可空), created_at`。
4. 提供 `write_audit(...)` 辅助，失败打日志不阻断主流程。

## D2. 鉴权
1. 集中权限函数，如 `require_role(*roles)` / `require_write`（legal+admin）。
2. 写操作挂护栏（至少）：上传/入库 job、审查/抽取/巡检启动、findings PATCH、报告导出可允许 viewer 下载已有报告但**不可**触发新审查（导出只读报告允许 viewer）、删合同、改文件夹、模型设置写、改他人角色。
3. viewer 调写接口 → 403，JSON 明确原因。
4. 前端：按 role 隐藏/禁用按钮（审查、入库、删除、改状态等）；不要只靠前端。

## D3. 审计埋点（至少）
登录失败、登录成功（可选）、上传/登记合同、开始审查、导出报告、findings 状态变更、改角色、删合同。`detail` 可含 contract_id、filename、finding_id、old→new role 等，**禁止**记密码或 API Key。

## D4. 管理页（简单即可）
- 路由如 `/admin` + `static/admin.html`（仅 admin）。
- 用户表：用户名、role、改角色（不可去掉最后一个 admin）。
- 最近审计列表（分页或最近 100 条），可按 action 筛。
- navbar：admin 可见「管理」。

## D5. 文档
README 短节：三角色含义、默认注册角色、如何指定首个 admin、审计记什么。


## 主要改动文件
- `store/storage.py`：users.role 迁移；`audit_logs`；CRUD；schema_version+1
- `store/auth.py`：注册默认 `legal`；login/`user_by_token` 带 role；登录失败审计
- `agent/app.py`：`require_role`；写接口护栏+审计；`/api/me` 返回 role；admin：用户列表、改角色、审计列表
- `static/main.js` + 各业务页：按 role 隐藏写操作；admin 导航「管理」
- 新建 `static/admin.html` + `/admin` 路由
- `README.md`（及可选 `WEB_PLAN.md`）短说明

## 明确不做
- 不做复杂 RBAC 引擎/组织架构/SSO
- 不改 findings 业务逻辑、不重做工作台
- 不强制改 demo 账号（可在管理页改角色）

## 验收
1. viewer 账号：能看工作台与 findings，点审查/改状态/删合同被拒（UI+API）。
2. legal：能走审查与改 findings；不能进管理页改角色。
3. admin：能改角色、看审计；改角色有审计记录。
4. 云端配置下完成上述（不必本地模型）。

## 回报
表结构、权限矩阵、新增路由、如何造三个角色账号测试。
