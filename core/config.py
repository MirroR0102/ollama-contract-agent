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
# 每一维度对应一类常见合同风险点，审查时逐项定向分析。
# missing_risk：若整份合同未检索到该维度相关约定（约定空白）时判定的风险等级；
# missing_tip：约定空白时输出的提示语（引导用户与对方详细确认）。
REVIEW_DIMENSIONS = [
    {
        "name": "违约金与赔偿",
        "query": "违约金、赔偿金、损失赔偿的约定条款",
        "desc": "检查违约金比例是否畸高、赔偿范围是否合理、是否约定损失计算方式",
        "missing_risk": "高",
        "missing_tip": "合同未约定违约金与损失赔偿条款：一旦发生违约，守约方将难以获得救济与赔偿，存在重大履约风险。建议补充违约金计算方式、赔偿范围与上限、损失证明要求，并与对方就此详细书面确认。",
    },
    {
        "name": "付款与结算",
        "query": "付款方式、付款期限、结算、发票、定金",
        "desc": "检查付款节点是否清晰、是否存在先付款后验收等不利安排",
        "missing_risk": "高",
        "missing_tip": "合同未约定付款方式、金额与结算节点：款项收付安排完全空白，是交易中最核心的财务风险点。建议务必与对方详细确认并书面写明：付款节点与金额、发票开具、结算依据、逾期付款责任，避免后续扯皮。",
    },
    {
        "name": "保密义务",
        "query": "保密条款、保密期限、保密范围、违约责任",
        "desc": "检查保密范围/期限是否明确、双向还是单向保密",
        "missing_risk": "高",
        "missing_tip": "合同未约定保密条款：双方在合作中接触的商业秘密、技术资料将缺乏法律保护，一旦泄露难以追责。建议补充保密信息范围、保密期限、例外情形及违约责任，并与对方确认。",
    },
    {
        "name": "合同解除与终止",
        "query": "合同解除、终止、单方解除权、解除条件",
        "desc": "检查解除条件是否苛刻、是否约定单方任意解除权",
        "missing_risk": "高",
        "missing_tip": "合同未约定解除与终止条款：届时只能适用法律的法定解除规则，程序与后果不确定。建议明确解除条件、书面通知程序、解除后的结算与资料返还安排，并与对方就此详细书面确认。",
    },
    {
        "name": "知识产权",
        "query": "知识产权、著作权、专利、软件、交付成果归属、归甲方所有、背景知识产权、使用许可",
        "desc": "检查成果/知识产权归属是否清晰，许可范围是否合理",
        "missing_risk": "高",
        "missing_tip": "合同未约定成果与知识产权归属：项目产出的软件、文档等权利归属空白，后续使用、修改、再许可极易引发权属纠纷。建议明确成果归属、背景知识产权许可范围与侵权担保，并与对方书面确认。",
    },
    {
        "name": "争议解决",
        "query": "争议解决、仲裁、诉讼、管辖法院、适用法律",
        "desc": "检查争议解决方式与管辖地是否约定清楚、是否对我方不利",
        "missing_risk": "高",
        "missing_tip": "合同未约定争议解决方式与管辖地：发生纠纷时将按法定管辖处理，可能不得不到对方所在地应诉，成本与不确定性高。建议明确诉讼或仲裁、管辖法院/仲裁机构及适用法律，并与对方书面确认。",
    },
    {
        "name": "责任限制与免责",
        "query": "责任限制、免责、不可抗力、赔偿上限",
        "desc": "检查是否存在过度免责/责任上限过低等风险",
        "missing_risk": "高",
        "missing_tip": "合同未约定责任限制、免责与不可抗力条款：一旦发生损失或不可抗力事件，双方责任边界不明，容易扩大争议。建议明确赔偿上限、间接损失排除与不可抗力处理机制，并与对方书面确认责任边界。",
    },
    {
        "name": "核心义务与违约责任",
        "query": "双方权利义务、交付、验收、违约责任、逾期",
        "desc": "检查核心义务是否对等、违约情形与后果是否明确",
        "missing_risk": "高",
        "missing_tip": "合同未清晰约定交付、验收等核心义务及违约后果：履约标准缺失，双方对「做到什么程度」没有共识，极易在交付与验收环节发生纠纷。建议逐项列明义务清单、时间节点、验收标准与违约责任，并与对方逐条确认。",
    },
]

# 单次送入大模型的合同上下文上限（字符），超出自动截断防止超长
MAX_CONTEXT_CHARS = 8000

# ================== 数据库（用户系统与合同库归属） ==================
# DB_ENGINE: sqlite（本地文件，免配置，默认）/ mysql（需填下方连接信息）
DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()
DB_PATH = os.getenv("DB_PATH", "./contract_ai.db")    # sqlite 数据库文件
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")           # mysql 连接信息
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "contract_ai")

# 预置演示账号：存放 contracts 演示库（保密协议/劳动合同/采购合同）的测试用户
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "demo")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo123")
