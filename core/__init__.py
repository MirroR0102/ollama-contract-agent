# core —— 配置和模型连接，不包含合同业务。
#   config.py            读 .env，导出 REVIEW_DIMENSIONS 等常量
#   ollama_conn.py       本地 ChatOllama / OllamaEmbeddings，断线重建
#   llm_provider.py      本地或云端二选一；嵌入始终走本地
#   embedding_client.py  给向量库用的薄封装，请每次 get_embedding()

