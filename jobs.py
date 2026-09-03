# -*- coding: utf-8 -*-
"""
jobs.py —— 轻量后台任务管理器（Contract AI 网页版）。

解决长任务（合同 8 维度审查等）与页面生命周期的耦合问题：
  * 任务在服务端后台线程运行，POST 提交后立即返回 job_id；
  * 任意页面都可 GET /stream 订阅进度（可多端同时看）；
  * 页面切换 / 关闭不直接杀死任务；当任务持续一段时间内
    「没有任何订阅者、且用户没有发起任何 API 请求」时，判定用户
    已离开浏览器，任务自动中止并释放模型锁（避免占着 GPU 白跑）。
"""
import os
import threading
import time
import uuid


def _friendly_error(msg: str) -> str:
    """把 Ollama / 推理层常见英文报错映射为可操作的中文提示。"""
    if "Failed to connect to Ollama" in msg or "connect to Ollama" in msg:
        return ("无法连接本地 Ollama 服务：Ollama 可能正在重启或已退出。"
                "请确认 Ollama 已启动后重试；若仍报错，请重启网页服务（python app.py）。")
    if "llama-server process has terminated" in msg or "CUDA error" in msg:
        return ("本地模型推理进程异常退出（CUDA / 显存相关）。"
                "请重启 Ollama 后重试；若频繁出现，建议更新显卡驱动或减小模型上下文。")
    if "out of memory" in msg.lower() or "allocate" in msg.lower():
        return "显存不足，无法完成推理。请关闭其他占用显存的程序后重试，或减小模型上下文长度。"
    return msg


class JobAbort(Exception):
    """任务被判定为「用户已离开」而主动中止。"""


class Job:
    def __init__(self, kind: str, filename: str, target, owner: str = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind          # review / extract
        self.filename = filename
        self.owner = owner        # 归属用户名（鉴权用）
        self.status = "running"   # running / done / error / aborted / paused
        self.error = None
        # 通用进度字段（worker 更新，SSE 订阅端读取）
        self.total = 0
        self.index = 0
        self.dim_name = ""
        self.text = ""            # 当前阶段的流式文本快照
        self.results = []         # review：已完成的维度结果
        self.payload = None       # extract：最终结构化结果
        # 订阅与保活
        self.subscribers = 0
        self.last_seen = time.time()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._target = target

    # ---------- 生命周期 ----------
    def start(self):
        self._thread.start()
        return self

    def _run(self):
        try:
            self._target(self)
            with self._lock:
                if self.status == "running":
                    self.status = "done"
        except JobAbort:
            with self._lock:
                if self.status != "paused":
                    self.status = "aborted"
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.status = "error"
                self.error = _friendly_error(f"{type(e).__name__}: {e}")

    def abort(self, reason: str = "任务已中止"):
        """彻底终止任务（不再可恢复）。"""
        with self._lock:
            if self.status in ("running", "paused"):
                self.status = "aborted"
                self.error = reason

    def pause(self):
        """暂停任务：保留已完成结果，可随时 resume 续跑。"""
        with self._lock:
            if self.status == "running":
                self.status = "paused"

    def resume(self):
        """从暂停处继续任务（跳过已完成维度）。"""
        with self._lock:
            if self.status == "paused":
                self.status = "running"
                self.last_seen = time.time()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    # ---------- 订阅 ----------
    def attach(self):
        with self._lock:
            self.subscribers += 1
            self.last_seen = time.time()

    def detach(self):
        with self._lock:
            self.subscribers = max(0, self.subscribers - 1)
            self.last_seen = time.time()

    def touch(self):
        with self._lock:
            self.last_seen = time.time()

    # ---------- 中止判定 ----------
    def should_stop(self):
        """运行中的任务是否应停止：被手动中止，或判定用户已离开。"""
        with self._lock:
            if self.status != "running":
                return True
            return (
                self.subscribers == 0
                and time.time() - self.last_seen > ABANDON_TIMEOUT
            )

    def abandoned(self):
        """仅「用户已离开」判定（供等待锁 / 保活逻辑使用）。"""
        with self._lock:
            return (
                self.status == "running"
                and self.subscribers == 0
                and time.time() - self.last_seen > ABANDON_TIMEOUT
            )

    # ---------- SSE 增量事件 ----------
    def events_since(self, last: dict):
        """基于游标 last 生成增量事件（type, payload），并更新游标。

        顺序保证：先发已完成的 dim_done（上一维度），再发 dim_start（新维度开始），
        最后才是新维度的 token 流。若顺序颠倒，前端会先建新维度卡又被 dim_done
        重建，导致「进行中卡片丢失、进度看似卡住」。
        """
        evs = []
        # ① 新完成的维度（结果）—— 必须先于下一个 dim_start
        nr = len(self.results)
        while last["nres"] < nr:
            evs.append(("dim_done", self.results[last["nres"]]))
            last["nres"] += 1
        # ② 维度推进（新维度开始）
        if self.index != last["index"]:
            if self.status == "running":
                evs.append(("dim_start", {
                    "index": self.index, "total": self.total, "name": self.dim_name,
                }))
            last["index"] = self.index
            last["textlen"] = 0  # 新阶段文本从零重新计数
        # ③ 流式文本增量（属于当前维度）
        t = self.text or ""
        if len(t) > last["textlen"]:
            evs.append(("token", {"text": t[last["textlen"]:]}))
            last["textlen"] = len(t)
        # 状态变化
        if self.status != last["status"]:
            last["status"] = self.status
            if self.status == "done" and self.payload is not None:
                evs.append(("result", self.payload))
        return evs

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "filename": self.filename,
            "status": self.status,
            "error": self.error,
            "total": self.total,
            "index": self.index,
            "dim_name": self.dim_name,
            "text": self.text or "",
            "results": list(self.results),
            "payload": self.payload,
        }


# 秒：任务「无订阅者且用户没有任何请求」持续该时长 → 判定用户已离开，中止任务
# （可用环境变量 JOB_ABANDON_TIMEOUT 覆盖，便于测试）
ABANDON_TIMEOUT = float(os.environ.get("JOB_ABANDON_TIMEOUT", "60"))

_jobs = {}
_jobs_lock = threading.Lock()


def create_job(kind: str, filename: str, target, owner: str = None) -> Job:
    with _jobs_lock:
        # 清理已完成旧任务，防止无限增长
        if len(_jobs) > 50:
            now = time.time()
            for j in list(_jobs.values()):
                if j.status != "running" and now - j.last_seen > 3600:
                    _jobs.pop(j.id, None)
        job = Job(kind, filename, target, owner=owner)
        _jobs[job.id] = job
        job.start()
        return job


def get_job(job_id: str):
    with _jobs_lock:
        return _jobs.get(job_id)


def keepalive_all():
    """用户发起了任意 API 请求 → 说明浏览器仍在使用 → 给后台任务续期。"""
    now = time.time()
    with _jobs_lock:
        for j in _jobs.values():
            if j.status == "running" and j.subscribers == 0:
                j.last_seen = now
