# -*- coding: utf-8 -*-
"""
vector_store.py —— Chroma 本地向量库管理
提供：入库（内容级去重，重复合同不重复写入）、检索器、库统计、清库。
数据全部持久化在本地 chroma_db 目录，断网可用。
"""
import hashlib
import os

from langchain_chroma import Chroma

from config import CHROMA_PATH
from document_loader import load_directory, load_document
from embedding_client import local_embedding


def _content_hash(text: str, owner: str = "") -> str:
    """对切片内容做 MD5（连同归属用户），用于判断是否已入库（增量去重）。
    不同用户的向量切片互不冲突：即使内容完全相同，也各自归属、互不可见。"""
    return hashlib.md5(f"{owner}\n{text}".encode("utf-8")).hexdigest()


def get_db() -> Chroma:
    """获取 / 初始化本地 Chroma 向量库实例。"""
    return Chroma(
        persist_directory=CHROMA_PATH,
        embedding_function=local_embedding,
    )


def _existing_hashes(db: Chroma, owner: str = "") -> set:
    """读取某归属用户已入库切片的内容哈希集合，用于增量去重。"""
    hashes = set()
    try:
        collection = db._collection
        where = {"owner": owner} if owner else None
        got = collection.get(where=where, include=["metadatas"])
        for meta in got.get("metadatas") or []:
            if meta and meta.get("hash"):
                hashes.add(meta["hash"])
    except Exception:  # noqa: BLE001  首次建库时集合可能为空
        pass
    return hashes


def add_documents(docs: list, owner: str = "") -> int:
    """
    向向量库写入切片（同一归属用户内自动跳过已存在内容）。
    - owner：归属用户名（切片 metadata 写入 owner，检索按此隔离）
    返回：实际新增条数。
    """
    db = get_db()
    existing = _existing_hashes(db, owner)
    new_docs = []
    for doc in docs:
        h = _content_hash(doc.page_content, owner)
        if h in existing:
            continue
        doc.metadata["hash"] = h
        doc.metadata["owner"] = owner
        new_docs.append(doc)
    if new_docs:
        db.add_documents(new_docs)
    return len(new_docs)


def add_file_to_kb(file_path: str, owner: str = "") -> int:
    """把单个合同文件入库（归 owner 名下），返回新增切片数。"""
    docs = load_document(file_path)
    added = add_documents(docs, owner=owner)
    print(f"  文档 {os.path.basename(file_path)} 共切 {len(docs)} 片，新增入库 {added} 片 (owner={owner})")
    return added


def add_dir_to_kb(dir_path: str, owner: str = "") -> int:
    """把目录下全部合同入库（归 owner 名下），返回新增切片总数。"""
    docs = load_directory(dir_path)
    if not docs:
        return 0
    added = add_documents(docs, owner=owner)
    print(f"  扫描切片 {len(docs)} 片，去重后新增入库 {added} 片 (owner={owner})")
    return added


def get_retriever(top_k: int = 3, sources: list = None, owner: str = None):
    """获取标准检索器（返回与问题最相关的 top_k 个合同片段）。
    - sources：可选，仅在这些合同文件（source 元数据）范围内检索；None/空 = 全部合同
    - owner：必填归属用户名（不传则返回空结果语义，调用方应总是传入当前用户）
    """
    db = get_db()
    filters = []
    if owner:
        filters.append({"owner": owner})
    if sources:
        filters.append({"source": {"$in": list(sources)}})
    filt = {"$and": filters} if len(filters) > 1 else (filters[0] if filters else None)
    kwargs: dict = {"k": top_k}
    if filt:
        kwargs["filter"] = filt
    return db.as_retriever(search_kwargs=kwargs)


def db_stats(owner: str = None) -> dict:
    """向量库统计：总切片数、涉及文件清单（可按归属用户过滤）。"""
    db = get_db()
    try:
        collection = db._collection
        where = {"owner": owner} if owner else None
        got = collection.get(where=where, include=["metadatas"])
    except Exception:  # noqa: BLE001
        return {"total_chunks": 0, "files": []}
    metas = got.get("metadatas") or []
    files = sorted({m.get("source") for m in metas if m})
    return {"total_chunks": len(metas), "files": files}


def clear_db() -> int:
    """清空向量库（删除全部切片），返回被清数量。"""
    db = get_db()
    try:
        collection = db._collection
        ids = collection.get(include=[])["ids"]
        if ids:
            collection.delete(ids=ids)
        return len(ids)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 清库失败：{e}")
        return 0


if __name__ == "__main__":
    # 自检：统计当前库状态（需先入库）
    print("当前向量库状态：", db_stats())
