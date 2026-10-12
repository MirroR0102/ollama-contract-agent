# -*- coding: utf-8 -*-
"""
document_loader.py —— txt / pdf 加载，以及全系统共用的条款分块器

load_document / load_directory 只接受 txt 与 pdf，给命令行入库和
vector_store.add_file_to_kb 用。网页上的 docx、图片、zip 由
store/docparse.py 抽出纯文本，再经 agent/intake_agent.make_docs
调用本文件的 text_splitter，所以切分规则仍然只有这一处。

每个切片的 metadata：
  source       文件名（网页侧应是磁盘唯一名 store_name）
  chunk_index  在该文件内的序号
owner、contract_id、hash 不在这里写，入库时由 vector_store 补上。
"""
import os

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

from core.config import CHUNK_SIZE, CHUNK_OVERLAP

# 合同专用分块器。RecursiveCharacterTextSplitter 按 separators 从左到右尝试：
# 先段落、再换行，然后才是「第」「条」和中文句读。目的是尽量在条款边界切开，
# 而不是在句子中间截断。长度用字符数（len），不用 token。
# 块大小来自 .env：CHUNK_SIZE 默认 450，CHUNK_OVERLAP 默认 80。
# 网页入库的 docx / 图片 OCR / zip 文本不走 load_document，
# 但 agent/intake_agent.make_docs 复用的就是下面这个 text_splitter。
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", "第", "条", "。", "；", "，", " ", ""],
    length_function=len,
)


def load_document(file_path: str) -> list:
    """
    加载单个合同文档并分块。
    返回：切片列表，每片 metadata 含 source(文件名) 与 chunk_index(序号)。
    """
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在：{file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        loader = TextLoader(file_path, encoding="utf-8")
    elif ext == ".pdf":
        loader = PyPDFLoader(file_path)
    else:
        raise ValueError(f"仅支持 txt / pdf 格式，当前文件：{ext}")

    raw_docs = loader.load()
    split_docs = text_splitter.split_documents(raw_docs)

    # 为每个切片补充来源信息
    source_name = os.path.basename(file_path)
    for i, doc in enumerate(split_docs):
        doc.metadata["source"] = source_name
        doc.metadata["chunk_index"] = i
    return split_docs


def load_directory(dir_path: str) -> list:
    """递归扫描目录下全部 txt/pdf 合同文档并分块，返回合并后的切片列表。"""
    all_docs: list = []
    if not os.path.isdir(dir_path):
        print(f"[警告] 合同目录不存在：{dir_path}")
        return all_docs
    for root, _, files in os.walk(dir_path):
        for name in sorted(files):
            if name.lower().endswith((".txt", ".pdf")):
                full = os.path.join(root, name)
                try:
                    docs = load_document(full)
                    all_docs.extend(docs)
                    print(f"  ✔ 已加载 {name}（{len(docs)} 片）")
                except Exception as e:  # noqa: BLE001
                    print(f"  ✘ 跳过 {name}：{e}")
    return all_docs


def read_plain_text(file_path: str) -> str:
    """读取整篇合同为纯文本（供合同审查 / 要素抽取直接使用）。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    if ext == ".pdf":
        from langchain_community.document_loaders import PyPDFLoader

        docs = PyPDFLoader(file_path).load()
        return "\n".join(d.page_content for d in docs)
    raise ValueError(f"仅支持 txt / pdf 格式，当前文件：{ext}")


if __name__ == "__main__":
    # 自检：打印一份合同的分块效果
    sample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contracts")
    files = [f for f in os.listdir(sample) if f.lower().endswith(".txt")]
    if files:
        first = os.path.join(sample, files[0])
        chunks = load_document(first)
        print(f"\n文件 {files[0]} 共切出 {len(chunks)} 片，预览前 2 片：\n")
        for c in chunks[:2]:
            print("-" * 60)
            print(c.page_content[:200])
