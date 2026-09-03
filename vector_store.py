# -*- coding: utf-8 -*-
"""
vector_store.py —— Chroma 本地向量库管理
提供：入库（内容级去重，重复合同不重复写入）、检索器、库统计、清库。
数据全部持久化在本地 chroma_db 目录，断网可用。
"""
import hashlib

from langchain_chroma import Chroma

from config import CHROMA_PATH
from document_loader import load_directory, load_document
from embedding_client import local_embedding


def _content_hash(text: str) -> str:
    """对切片内容做 MD5，用于判断是否已入库（增量去重）。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def get_db() -> Chroma:
    """获取 / 初始化本地 Chroma 向量库实例。"""
    return Chroma(
        persist_directory=CHROMA_PATH,
        embedding_function=local_embedding,
    )


def _existing_hashes(db: Chroma) -> set:
    """读取库中已有切片的内容哈希集合，用于增量去重。"""
    hashes = set()
    try:
        collection = db._collection
        got = collection.get(include=["metadatas"])
        for meta in got.get("metadatas") or []:
            if meta and meta.get("hash"):
                hashes.add(meta["hash"])
    except Exception:  # noqa: BLE001  首次建库时集合可能为空
        pass
    return hashes


def add_documents(docs: list) -> int:
    """
    向向量库写入切片（自动跳过已存在内容）。
    返回：实际新增条数。
    """
    db = get_db()
    existing = _existing_hashes(db)
    new_docs = []
    for doc in docs:
        h = _content_hash(doc.page_content)
        if h in existing:
            continue
        doc.metadata["hash"] = h
        new_docs.append(doc)
    if new_docs:
        db.add_documents(new_docs)
    return len(new_docs)


def add_file_to_kb(file_path: str) -> int:
    """把单个合同文件入库，返回新增切片数。"""
    docs = load_document(file_path)
    added = add_documents(docs)
    print(f"  文档 {file_path} 共切 {len(docs)} 片，新增入库 {added} 片")
    return added


def add_dir_to_kb(dir_path: str) -> int:
    """把目录下全部合同入库，返回新增切片总数。"""
    docs = load_directory(dir_path)
    if not docs:
        return 0
    added = add_documents(docs)
    print(f"  扫描切片 {len(docs)} 片，去重后新增入库 {added} 片")
    return added


def get_retriever(top_k: int = 3):
    """获取标准检索器（返回与问题最相关的 top_k 个合同片段）。"""
    db = get_db()
    return db.as_retriever(search_kwargs={"k": top_k})


def db_stats() -> dict:
    """向量库统计：总切片数、涉及文件清单。"""
    db = get_db()
    try:
        collection = db._collection
        total = collection.count()
        got = collection.get(include=["metadatas"])
    except Exception:  # noqa: BLE001
        return {"total_chunks": 0, "files": []}
    files = sorted({m.get("source") for m in (got.get("metadatas") or []) if m})
    return {"total_chunks": total, "files": files}


def clear_db() -> int:
    """清空向量库（删除全部切片），返回被清数量。"""
    db = get_db()
    try:
        total = db._collection.count()
        db._collection.delete(where=None)
        return total
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 清库失败：{e}")
        return 0


if __name__ == "__main__":
    # 自检：统计当前库状态（需先入库）
    print("当前向量库状态：", db_stats())
