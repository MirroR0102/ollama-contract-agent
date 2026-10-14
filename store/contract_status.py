# -*- coding: utf-8 -*-
"""合同生命周期状态机。

合法流转集中在这里。API 遇到非法流转返回 400；后台任务回写失败只打日志，不把任务打崩。

不改状态的任务（在 jobs 同步里直接跳过）：
  * ingest_analyze：只分析暂存文件，此时还没有合同行
  * inspect：一次巡检覆盖该用户全部合同，不归属某一份
  * ingest_commit：一份任务可能写入多份合同，由任务体按每份合同回写
"""
from store.storage import get_store

STATUSES = (
    "uploaded",
    "ingesting",
    "ready",
    "reviewing",
    "reviewed",
    "archived",
    "failed",
)

# 旧库升级时已有合同默认 ready，见 storage 迁移注释。
_NEXT = {
    "uploaded": {"ingesting", "ready", "failed"},
    "ingesting": {"ready", "failed"},
    "ready": {"ingesting", "reviewing", "archived"},
    "reviewing": {"reviewed", "failed"},
    "reviewed": {"reviewing", "archived"},
    "failed": {"uploaded", "ingesting", "ready", "reviewing"},
    "archived": {"ready", "reviewed"},
}

# 单合同任务：开始状态、成功状态。失败一律 failed。
_JOB_PHASE = {
    "review": ("reviewing", "reviewed"),
    "extract": ("reviewing", "reviewed"),
    "report_summary": ("reviewing", "reviewed"),
}

STATUS_LABELS = {
    "uploaded": "待入库",
    "ingesting": "入库中",
    "ready": "可审查",
    "reviewing": "审查中",
    "reviewed": "已审查",
    "archived": "已归档",
    "failed": "失败",
}


class IllegalTransition(ValueError):
    """当前状态不能变到目标状态。"""


def can_transition(src: str, dst: str) -> bool:
    src = src or "ready"
    if src == dst:
        return True
    return dst in _NEXT.get(src, ())


def actions_for(status: str) -> list:
    """工作台可展示的操作。前端据此显示按钮，真正拦截仍以状态机为准。"""
    s = status or "ready"
    ops = []
    if s in ("uploaded", "failed"):
        ops.append("ingest")
    if s in ("ready", "reviewed", "failed"):
        ops.append("review")
    if s in ("uploaded", "ready", "reviewed", "failed", "archived"):
        ops.extend(["chat", "kb"])
    if s in ("ready", "reviewed"):
        ops.append("archive")
    if s == "archived":
        ops.append("unarchive")
    if s == "failed":
        ops.append("retry")
    if s in ("ingesting", "reviewing"):
        ops.append("job")
    return ops


def change_contract(user_id, contract_id, status, *, job_id=None, error="") -> bool:
    """按状态机更新一份合同。找不到或非法流转时返回 False，不抛给调用方。"""
    try:
        return bool(get_store().set_contract_status(
            user_id, contract_id, status,
            job_id=job_id, error=error, touch_job=True, touch_error=True))
    except IllegalTransition as e:
        print(f"[status] 跳过非法流转 contract={contract_id}: {e}", flush=True)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[status] 回写失败 contract={contract_id}: {e}", flush=True)
        return False


def _resolve_job_contract(job):
    """优先 contract_id，其次磁盘文件名，再退到唯一标题。找不到返回 None。"""
    owner = getattr(job, "owner", None)
    if not owner:
        print(f"[status] 任务 {getattr(job, 'id', '')} 无归属用户，跳过回写", flush=True)
        return None
    store = get_store()
    user = store.get_user_by_name(owner)
    if not user:
        print(f"[status] 找不到用户 {owner}，跳过回写", flush=True)
        return None
    cid = getattr(job, "contract_id", None)
    if cid:
        rec = store.get_contract(user["id"], cid)
        if rec:
            return user, rec
        print(f"[status] contract_id={cid} 不属于 {owner}，改按文件名查找", flush=True)
    filename = getattr(job, "filename", None) or ""
    if filename:
        rec = store.get_contract_by_store(user["id"], filename)
        if rec:
            return user, rec
        titled = store.find_contracts_by_title(user["id"], filename) or []
        if len(titled) == 1:
            rec = store.get_contract(user["id"], titled[0]["id"])
            if rec:
                return user, rec
    print(f"[status] 未找到合同 kind={getattr(job, 'kind', '')} file={filename}", flush=True)
    return None


def sync_job_contract(job, phase: str) -> None:
    """任务开始 / 成功 / 失败时回写单份合同。不适用的 kind 在此跳过。"""
    spec = _JOB_PHASE.get(getattr(job, "kind", ""))
    if not spec:
        return
    found = _resolve_job_contract(job)
    if not found:
        return
    user, rec = found
    running, done = spec
    if phase == "start":
        status, job_id, error = running, job.id, ""
    elif phase == "ok":
        status, job_id, error = done, None, ""
    else:
        err = (getattr(job, "error", None) or "任务失败")
        status, job_id, error = "failed", None, str(err)[:500]
    change_contract(user["id"], rec["id"], status, job_id=job_id, error=error)
