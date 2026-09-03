# -*- coding: utf-8 -*-
"""
embedding_client.py —— 本地向量嵌入模型封装（连接经 ollama_conn 统一管理）
把本地下载的 bge-m3 等嵌入模型封装成 LangChain 标准 embedding 对象，
供 Chroma 向量化检索使用，全程离线、不依赖任何云服务。

连接实例由 ollama_conn 统一维护：Ollama 重启导致连接失效时，重试逻辑会
自动重建实例（自愈）。vector_store 应通过 get_embedding() 获取「最新」实例
再绑定进 Chroma，切勿持有固定实例快照（快照在重建后会失效）。
"""
from ollama_conn import get_emb, reset_emb


def get_embedding():
    """返回当前最新 Embedding 实例（Chroma 向量化绑定用）。"""
    return get_emb()


def reset_embedding():
    """重建 Embedding 实例（连接失效时由重试逻辑调用）。"""
    return reset_emb()


# 兼容保留（仅供命令行自检等一次性使用；正式代码请用 get_embedding()）
local_embedding = get_embedding()


if __name__ == "__main__":
    # 自检：验证本地嵌入服务是否连通
    vector = local_embedding.embed_query("测试：本合同违约金条款")
    print(f"嵌入模型连接成功，向量维度 = {len(vector)}")
