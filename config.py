# -*- coding: utf-8 -*-
"""
config.py —— 全局统一配置读取
从 .env 读取 Ollama 服务地址、模型名、向量库与分块参数，
所有模块通过 from config import ... 使用同一份配置。
"""
import os
import sys

from dotenv import load_dotenv

# Windows 控制台默认 GBK 编码，此处统一将输出流转为 UTF-8，
# 避免打印中文/特殊符号（如 ✔、🔴）时抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

# 加载项目根目录 .env 配置（所有运行入口均从项目根启动）
load_dotenv()

# ---------- Ollama 服务配置 ----------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLM_MODEL_NAME = os.getenv("LLM_MODEL", "qwen2.5:7b")
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL", "bge-m3")

# ---------- 向量库与文档配置 ----------
CHROMA_PATH = os.getenv("CHROMA_PATH", "./chroma_db")
CONTRACTS_DIR = os.getenv("CONTRACTS_DIR", "./contracts")

# ---------- 文档分块参数 ----------
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "450"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "80"))

# ---------- 合同审查维度配置（答辩可重点展开） ----------
# 每一维度对应一类常见合同风险点，审查时逐项定向分析
REVIEW_DIMENSIONS = [
    {
        "name": "违约金与赔偿",
        "query": "违约金、赔偿金、损失赔偿的约定条款",
        "desc": "检查违约金比例是否畸高、赔偿范围是否合理、是否约定损失计算方式",
    },
    {
        "name": "付款与结算",
        "query": "付款方式、付款期限、结算、发票、定金",
        "desc": "检查付款节点是否清晰、是否存在先付款后验收等不利安排",
    },
    {
        "name": "保密义务",
        "query": "保密条款、保密期限、保密范围、违约责任",
        "desc": "检查保密范围/期限是否明确、双向还是单向保密",
    },
    {
        "name": "合同解除与终止",
        "query": "合同解除、终止、单方解除权、解除条件",
        "desc": "检查解除条件是否苛刻、是否约定单方任意解除权",
    },
    {
        "name": "知识产权",
        "query": "知识产权、著作权、专利、软件、成果归属",
        "desc": "检查成果/知识产权归属是否清晰，许可范围是否合理",
    },
    {
        "name": "争议解决",
        "query": "争议解决、仲裁、诉讼、管辖法院、适用法律",
        "desc": "检查争议解决方式与管辖地是否约定清楚、是否对我方不利",
    },
    {
        "name": "责任限制与免责",
        "query": "责任限制、免责、不可抗力、赔偿上限",
        "desc": "检查是否存在过度免责/责任上限过低等风险",
    },
    {
        "name": "核心义务与违约责任",
        "query": "双方权利义务、交付、验收、违约责任、逾期",
        "desc": "检查核心义务是否对等、违约情形与后果是否明确",
    },
]

# 单次送入大模型的合同上下文上限（字符），超出自动截断防止超长
MAX_CONTEXT_CHARS = 8000
