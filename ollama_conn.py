# -*- coding: utf-8 -*-
"""
ollama_conn.py —— Ollama 连接管理 + 失效自动重建（自愈）

背景：网页服务是常驻进程，启动时与 Ollama 建立的 HTTP 长连接在 Ollama
重启/退出后变为“死连接”。复用旧连接会抛 Failed to connect to Ollama，
导致审查/问答报错且需手动重启网页服务。

本模块把所有模型实例集中管理，并在捕获连接类错误后自动重建
ChatOllama / OllamaEmbeddings 实例再重试，服务无需人工干预即可自愈。
"""
import threading
import time

from langchain_ollama import ChatOllama, OllamaEmbeddings

from config import OLLAMA_HOST, LLM_MODEL_NAME, EMBED_MODEL_NAME

# ---------- 重试策略 ----------
MAX_RETRY = 2          # 连接失效后最多重建并重试 2 次（含首建共 3 次尝试）
BASE_DELAY = 1.5       # 每次重试前的等待秒数（逐次递增：1.5s / 3s）
_REQUEST_TIMEOUT = 600  # 单次模型请求最长 10 分钟（审查长生成不被误判超时）

_lock = threading.Lock()
_llm = None
_emb = None


def _build_llm():
    """构造全新的 ChatOllama 实例（每次重建都拿到全新 HTTP 连接）。"""
    return ChatOllama(
        base_url=OLLAMA_HOST,
        model=LLM_MODEL_NAME,
        temperature=0,
        num_ctx=4096,      # 与原配置一致：控制上下文窗口，防 6GB 显存溢出
        num_predict=2048,  # 与原配置一致：限制单次最长输出
        request_timeout=_REQUEST_TIMEOUT,
    )


def _build_emb():
    """构造全新的 OllamaEmbeddings 实例。"""
    return OllamaEmbeddings(
        base_url=OLLAMA_HOST,
        model=EMBED_MODEL_NAME,
    )


def _ensure():
    """惰性初始化（首次访问才创建实例，避免模块导入期就连 Ollama）。"""
    global _llm, _emb
    with _lock:
        if _llm is None:
            _llm = _build_llm()
        if _emb is None:
            _emb = _build_emb()


def get_llm():
    """返回当前 LLM 实例（供 LangChain Agent / 直接调用）。"""
    _ensure()
    with _lock:
        return _llm


def get_emb():
    """返回当前 Embedding 实例（供 Chroma 向量化使用）。"""
    _ensure()
    with _lock:
        return _emb


def reset_llm():
    """重建 LLM 实例（旧连接已失效时调用），返回新实例。"""
    global _llm
    new_llm = _build_llm()
    with _lock:
        _llm = new_llm
    print("  [重连] 已重建 ChatOllama 连接实例", flush=True)
    return new_llm


def reset_emb():
    """重建 Embedding 实例（旧连接已失效时调用），返回新实例。"""
    global _emb
    new_emb = _build_emb()
    with _lock:
        _emb = new_emb
    print("  [重连] 已重建 OllamaEmbeddings 连接实例", flush=True)
    return new_emb


def is_conn_error(exc) -> bool:
    """判断异常是否属于『连接失效 / 服务不可达』类——这类错误可通过重建连接自愈。

    注意：显存不足/模型崩溃等其它错误不在此列，会如实上抛并提示用户。
    """
    msg = str(exc)
    low = msg.lower()
    keys = (
        "failed to connect to ollama",
        "connect to ollama",
        "connection error",
        "connecterror",
        "remoteprotocolerror",
        "connection reset",
        "connection refused",
        "connect call failed",
        "econnrefused",
        "econnreset",
        "networkerror",
        "readtimeout",
        "read timed out",
        "llama-server process has terminated",
        "ollama is not running",
        "connect failed",
    )
    return any(k in low for k in keys)


def retry_llm_call(fn, retries: int = MAX_RETRY):
    """执行一次 LLM 调用 fn()；若抛连接类错误，重建 LLM 实例后重试。

    fn 必须是「无参或自带闭包参数、每次调用都重新发起请求」的函数，
    例如 lambda: get_llm().invoke([...])——重试时会拿到重建后的新实例。
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt < retries and is_conn_error(e):
                time.sleep(BASE_DELAY * (attempt + 1))
                reset_llm()
                continue
            raise
    raise RuntimeError("unreachable")


def retry_emb_call(fn, retries: int = MAX_RETRY):
    """执行一次 Embedding 相关调用 fn()；若抛连接类错误，重建 Embedding 后重试。

    fn 每次调用都应重新构建 Chroma/检索器（绑定 get_emb() 的最新实例）。
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt < retries and is_conn_error(e):
                time.sleep(BASE_DELAY * (attempt + 1))
                reset_emb()
                continue
            raise
    raise RuntimeError("unreachable")
