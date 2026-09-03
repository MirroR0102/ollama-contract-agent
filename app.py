# -*- coding: utf-8 -*-
"""
app.py —— Contract AI 网页版后端（FastAPI + SSE 流式）
将本地化合同审查系统的全部能力封装为 HTTP 接口：
  知识库问答 / Agent 对话 / 合同审查 / 要素抽取 / 合同入库
全程本地 Ollama 推理，零云依赖。
启动：python app.py  →  http://localhost:8000
"""
import asyncio
import json
import os
import queue
import threading

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel

from agent_run import kb_agent
from config import CONTRACTS_DIR, LLM_MODEL_NAME
from contract_analyzer import REVIEW_DIMENSIONS, analyze_dimension
from contract_kb import _KB_PROMPT, _format_context, llm
from element_extractor import extract_elements
from vector_store import add_file_to_kb, db_stats, get_retriever

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title="Contract AI · 本地化智能合同审查系统")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Ollama 单模型串行：全局锁避免并发推理错乱
MODEL_LOCK = threading.Lock()


# ==================== SSE 基础设施 ====================
def _sse(event: dict) -> str:
    """序列化一个 SSE data 事件。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class _ClientGone(Exception):
    """客户端连接已断开，主动中止当前推理。"""


async def sse_generator(worker):
    """
    通用 SSE 生成器：在后台线程运行 worker(send, stop)，
    worker 通过 send(type, payload) 推送事件，主线程逐条 yield。
    worker 结束时自动补发 done 事件；异常自动补发 error 事件。

    关键：客户端断开（刷新 / 关闭页面）时，本生成器被 asyncio 取消，
    finally 中会置 stop 事件；worker 在每轮循环 / 每个 token 处检查
    stop 即可及时中止推理，尽快释放 MODEL_LOCK，避免整站假死。
    """
    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def send(etype: str, payload=None):
        q.put((etype, payload))

    def run():
        try:
            worker(send, stop)
        except _ClientGone:
            pass  # 客户端已断开，静默退出
        except Exception as e:  # noqa: BLE001
            try:
                q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
            except Exception:
                pass
        finally:
            try:
                q.put(("__end__", None))
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    try:
        while True:
            try:
                etype, payload = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.03)  # 轮询间隔：让取消能被及时感知
                continue
            if etype == "__end__":
                break
            yield _sse({"type": etype, "data": payload})
        yield _sse({"type": "done"})
    finally:
        stop.set()  # 正常结束或客户端断开都会通知 worker 停止


def sse_response(worker) -> StreamingResponse:
    return StreamingResponse(
        sse_generator(worker),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== 工具函数 ====================
def _list_all_files() -> list:
    """列出 contracts 与 uploads 两个目录的合同文件。"""
    files = []
    for d in (CONTRACTS_DIR, UPLOAD_DIR):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".txt", ".pdf")):
                    files.append({"name": f, "dir": os.path.basename(d)})
    return files


def _resolve_anywhere(name: str):
    """在 contracts/uploads 目录定位文件，返回绝对路径或 None。"""
    for d in (CONTRACTS_DIR, UPLOAD_DIR):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    if os.path.isfile(name):
        return os.path.abspath(name)
    return None


# ==================== 页面路由 ====================
@app.get("/")
def page_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/kb")
def page_kb():
    return FileResponse(os.path.join(STATIC_DIR, "kb.html"))


@app.get("/review")
def page_review():
    return FileResponse(os.path.join(STATIC_DIR, "review.html"))


@app.get("/ingest")
def page_ingest():
    return FileResponse(os.path.join(STATIC_DIR, "ingest.html"))


# ==================== 数据接口 ====================
@app.get("/api/files")
def api_files():
    """合同清单 + 系统状态（各页面共用）。"""
    return {
        "files": _list_all_files(),
        "model": LLM_MODEL_NAME,
        "stats": db_stats(),
    }


# ==================== SSE 流式接口 ====================
class KBQueryBody(BaseModel):
    question: str
    top_k: int = 3


@app.post("/api/kb/query")
def api_kb_query(body: KBQueryBody):
    """知识库 RAG 问答（纯检索 + 流式回答，带证据片段）。"""

    def worker(send, stop):
        with MODEL_LOCK:
            retriever = get_retriever(body.top_k)
            docs = retriever.invoke(body.question)
            if not docs:
                send("message", {"text": "知识库未检索到相关合同内容，请先到「合同入库」页导入合同。"})
                return
            # ① 证据片段
            send("evidence", {
                "items": [
                    {"index": i + 1,
                     "source": d.metadata.get("source", "未知"),
                     "content": d.page_content}
                    for i, d in enumerate(docs)
                ]
            })
            # ② 流式回答
            prompt = _KB_PROMPT.format(context=_format_context(docs), question=body.question)
            for chunk in llm.stream([HumanMessage(content=prompt)]):
                if stop.is_set():
                    return
                piece = chunk.content or ""
                if piece:
                    send("token", {"text": piece})

    return sse_response(worker)


class ChatBody(BaseModel):
    message: str
    thread_id: str = "web_default"


@app.post("/api/chat")
def api_chat(body: ChatBody):
    """Agent 多轮对话（工具调用事件 + 流式回答）。"""

    def worker(send, stop):
        with MODEL_LOCK:
            tool_names: dict = {}
            for chunk, metadata in kb_agent.stream(
                {"messages": [HumanMessage(content=body.message)]},
                config={"configurable": {"thread_id": body.thread_id}},
                stream_mode="messages",
            ):
                if stop.is_set():
                    return
                # 工具调用决策
                tcc = getattr(chunk, "tool_call_chunks", None)
                if tcc:
                    for tc in tcc:
                        idx = tc.get("index", 0)
                        piece = (tc.get("name") or "").strip()
                        if piece and idx not in tool_names:
                            tool_names[idx] = piece
                            send("tool_call", {"name": piece})
                # 文本 / 工具结果
                text = chunk.content or ""
                if isinstance(chunk, AIMessageChunk):
                    if text:
                        send("token", {"text": text})
                elif isinstance(chunk, ToolMessage):
                    send("tool_result", {"text": str(text)[:200]})

    return sse_response(worker)


class FileBody(BaseModel):
    filename: str


@app.post("/api/analyze")
def api_analyze(body: FileBody):
    """合同 8 维度审查：逐维度流式输出 + 维度完成事件。"""

    def worker(send, stop):
        path = _resolve_anywhere(body.filename)
        if not path:
            send("error", {"message": f"未找到合同文件：{body.filename}"})
            return
        with MODEL_LOCK:
            add_file_to_kb(path)  # 确保已入库（幂等）
            total = len(REVIEW_DIMENSIONS)
            for i, dim in enumerate(REVIEW_DIMENSIONS, 1):
                if stop.is_set():
                    return  # 客户端已离开：中止剩余维度，尽快释放模型锁
                send("dim_start", {"index": i, "total": total, "name": dim["name"]})

                def emit(t, _s=send, _stop=stop):
                    if _stop.is_set():
                        raise _ClientGone()
                    _s("token", {"text": t})

                try:
                    result = analyze_dimension(
                        os.path.basename(path), dim, stream=False, emit=emit,
                    )
                except _ClientGone:
                    return  # 客户端已断开，中止当前维度
                send("dim_done", result)

    return sse_response(worker)


@app.post("/api/extract")
def api_extract(body: FileBody):
    """合同关键要素抽取：流式 JSON + 结果事件。"""

    def worker(send, stop):
        path = _resolve_anywhere(body.filename)
        if not path:
            send("error", {"message": f"未找到合同文件：{body.filename}"})
            return
        with MODEL_LOCK:
            send("message", {"text": "正在抽取关键要素...\n"})

            def emit(t, _s=send, _stop=stop):
                if _stop.is_set():
                    raise _ClientGone()
                _s("token", {"text": t})

            try:
                data = extract_elements(path, stream=False, emit=emit)
            except _ClientGone:
                return  # 客户端已断开，中止抽取
            send("result", {"data": data})

    return sse_response(worker)


# ==================== 上传入库 ====================
@app.post("/api/ingest")
async def api_ingest(file: UploadFile = File(...)):
    """上传合同文件并入库。"""
    name = file.filename or "contract.txt"
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".txt", ".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 txt / pdf 格式")
    safe = os.path.basename(name.replace("\\", "/")).replace("/", "_")
    base, e = os.path.splitext(safe)
    dest = os.path.join(UPLOAD_DIR, safe)
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(UPLOAD_DIR, f"{base}_{counter}{e}")
        counter += 1

    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    added = add_file_to_kb(dest)
    return {"ok": True, "saved": os.path.basename(dest), "added_chunks": added}


if __name__ == "__main__":
    import uvicorn

    print("=" * 56)
    print("  Contract AI · 本地化智能合同审查系统（网页版）")
    print(f"  模型：{LLM_MODEL_NAME}   服务：http://localhost:8000")
    print("  Ctrl+C 退出")
    print("=" * 56)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
