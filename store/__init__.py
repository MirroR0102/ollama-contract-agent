# store —— 数据层，不直接调大模型（分块、解析、向量、账号、会话旁路）。
#   document_loader.py   txt/pdf 加载 + 全系统共用的 text_splitter
#   docparse.py          网页多格式抽文本（含图片 OCR、zip）
#   vector_store.py      Chroma：owner + contract_id 隔离
#   storage.py           SQLite / MySQL：用户、会话、文件夹、合同记录
#   auth.py              PBKDF2 密码与 token
#   session_context.py   进程内：thread_id → 用户 / 合同范围
#   bootstrap.py         启动时建 demo 账号并导入 contracts/ 样例

