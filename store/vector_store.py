# -*- coding: utf-8 -*-
"""
vector_store.py —— Chroma 本地向量库管理
提供：入库（按 owner+contract_id 域去重）、检索器、统计、删除、清库。

隔离模型（v2）：每个用户的每份合同拥有独立向量域。切片 metadata 含
  owner       —— 用户名（跨用户隔离）
  contract_id —— 合同记录 id（同用户内、跨合同隔离；同内容不同合同各自建库）
  source      —— 磁盘唯一文件名 store_name（前端按此精确限定单合同）
  hash        —— md5(owner|contract_id|text)，用于该合同内的增量去重
数据全部持久化在本地 chroma_db 目录，断网可用。
"""
import hashlib
import os

from langchain_chroma import Chroma

from core.config import CHROMA_PATH
from core.embedding_client import get_embedding
from core.ollama_conn import retry_emb_call
from store.document_loader import load_directory, load_document


def _content_hash(text: str, owner: str = "", contract_id: str = "") -> str:
    """对切片内容做 MD5（连同 owner 与 contract_id），用于增量去重。
    不同用户、不同合同互不冲突：同名同内容的两份合同（contract_id 不同）
    也各自独立入库，互不覆盖。"""
    return hashlib.md5(
        f"{owner}|{contract_id}|{text}".encode("utf-8")).hexdigest()


def get_db() -> Chroma:
    """获取 / 初始化本地 Chroma 向量库实例（绑定最新 embedding 实例）。"""
    return Chroma(
        persist_directory=CHROMA_PATH,
        embedding_function=get_embedding(),
    )


def _mk_where(pairs: dict):
    """构造 Chroma where 过滤：多条件必须用 $and 包装
    （Chroma 顶层同时给多个键会报 Expected where to have exactly one operator）。"""
    conds = [{k: v} for k, v in pairs.items() if v]
    if not conds:
        return None
    if len(conds) == 1:
        return conds[0]
    return {"$and": conds}


def _existing_hashes(db: Chroma, owner: str = "", contract_id: str = "") -> set:
    """读取某合同域已入库切片的内容哈希集合，用于该域内增量去重。"""
    hashes = set()
    try:
        collection = db._collection
        where = _mk_where({"owner": owner, "contract_id": str(contract_id) if contract_id else ""})
        got = collection.get(where=where, include=["metadatas"])
        for meta in got.get("metadatas") or []:
            if meta and meta.get("hash"):
                hashes.add(meta["hash"])
    except Exception:  # noqa: BLE001  首次建库时集合可能为空
        pass
    return hashes


def _add_documents_once(docs: list, owner: str, contract_id: str) -> int:
    """单次入库（embedding 连接失效可能抛错，由外层自动重建重试）。"""
    db = get_db()
    existing = _existing_hashes(db, owner, contract_id)
    new_docs = []
    for doc in docs:
        h = _content_hash(doc.page_content, owner, contract_id)
        if h in existing:
            continue
        doc.metadata["hash"] = h
        doc.metadata["owner"] = owner
        doc.metadata["contract_id"] = str(contract_id) if contract_id else ""
        new_docs.append(doc)
    if new_docs:
        db.add_documents(new_docs)
    return len(new_docs)


def add_documents(docs: list, owner: str = "", contract_id: str = "") -> int:
    """
    向向量库写入切片（在 owner+contract_id 域内跳过已存在内容）；embedding
    连接失效时自动重建实例并重试（自愈）。
    - owner：归属用户名（切片 metadata 写入 owner，检索按此隔离）
    - contract_id：合同记录 id；不传则退回按 owner 整体去重（旧行为）
    返回：实际新增条数。
    """
    return retry_emb_call(lambda: _add_documents_once(docs, owner, contract_id))


def add_file_to_kb(file_path: str, owner: str = "", contract_id: str = "") -> int:
    """把单个合同文件入库（归 owner 名下某合同域），返回新增切片数。
    - 切片 source = os.path.basename(file_path)（应为磁盘唯一名 store_name）
    """
    docs = load_document(file_path)
    added = add_documents(docs, owner=owner, contract_id=contract_id)
    print(f"  文档 {os.path.basename(file_path)} 共切 {len(docs)} 片，"
          f"新增入库 {added} 片 (owner={owner}, contract={contract_id})")
    return added


def add_dir_to_kb(dir_path: str, owner: str = "", contract_id: str = "") -> int:
    """把目录下全部合同入库（归 owner 名下），返回新增切片总数。"""
    docs = load_directory(dir_path)
    if not docs:
        return 0
    added = add_documents(docs, owner=owner, contract_id=contract_id)
    print(f"  扫描切片 {len(docs)} 片，去重后新增入库 {added} 片 (owner={owner})")
    return added


def delete_contract_vectors(owner: str, contract_id) -> int:
    """删除某用户某合同的全部向量切片，返回删除条数。"""
    db = get_db()
    try:
        collection = db._collection
        where = _mk_where({"owner": owner,
                           "contract_id": str(contract_id) if contract_id else ""})
        ids = collection.get(where=where, include=[])["ids"]
        if ids:
            collection.delete(ids=ids)
        return len(ids)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 删除合同向量失败：{e}")
        return 0


def count_vectors(owner=None, source=None, contract_id=None) -> int:
    """统计满足 owner / source / contract_id 条件的切片数（审查前预检用）。
    向量库读取异常时抛出，由调用方转为中文错误提示。"""
    db = get_db()
    where = _mk_where({"owner": owner or "", "source": source or "",
                       "contract_id": str(contract_id) if contract_id else ""})
    try:
        ids = db._collection.get(where=where, include=[])["ids"]
        return len(ids or [])
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"向量库读取失败：{e}") from e


def get_retriever(top_k: int = 3, sources: list = None, owner: str = None):
    """获取标准检索器（返回与问题最相关的 top_k 个合同片段）。
    - sources：可选，仅在这些合同文件（source=磁盘名）范围内检索；None/空 = 全部合同
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
    """向量库统计：该用户全部合同域切片总数 + 分合同明细（可按归属用户过滤）。
    返回：{total_chunks, files[磁盘名去重], contracts:[{contract_id, name, chunks}]}
    """
    db = get_db()
    try:
        collection = db._collection
        where = {"owner": owner} if owner else None
        got = collection.get(where=where, include=["metadatas"])
    except Exception:  # noqa: BLE001
        return {"total_chunks": 0, "files": [], "contracts": []}
    metas = got.get("metadatas") or []
    files = sorted({m.get("source") for m in metas if m and m.get("source")})
    # 按 contract_id 聚合（兼容旧切片无 contract_id → 归 ''）
    agg: dict = {}
    for m in metas:
        if not m:
            continue
        cid = str(m.get("contract_id") or "")
        item = agg.setdefault(cid, {"contract_id": cid,
                                    "name": m.get("source") or "", "chunks": 0})
        item["chunks"] += 1
    contracts = sorted(agg.values(), key=lambda x: -x["chunks"])
    return {"total_chunks": len(metas), "files": files, "contracts": contracts}


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
