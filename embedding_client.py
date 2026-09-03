# -*- coding: utf-8 -*-
"""
embedding_client.py —— Ollama 本地向量嵌入模型封装
把本地下载的 bge-m3 等嵌入模型封装成 LangChain 标准 embedding 对象，
供 Chroma 向量化检索使用，全程离线、不依赖任何云服务。
"""
from langchain_ollama import OllamaEmbeddings

from config import OLLAMA_HOST, EMBED_MODEL_NAME

# 实例化本地嵌入模型（base_url 指向本地 Ollama 服务端口 11434）
local_embedding = OllamaEmbeddings(
    base_url=OLLAMA_HOST,
    model=EMBED_MODEL_NAME,
)

if __name__ == "__main__":
    # 自检：验证本地嵌入服务是否连通
    vector = local_embedding.embed_query("测试：本合同违约金条款")
    print(f"嵌入模型 [{EMBED_MODEL_NAME}] 连接成功，向量维度 = {len(vector)}")
