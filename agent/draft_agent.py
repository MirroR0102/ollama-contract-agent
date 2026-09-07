# -*- coding: utf-8 -*-
"""
draft_agent.py —— 合同起草智能体

根据用户自然语言需求（含标的/金额/期限/违约责任等）流式生成一份结构完整的
中文合同草案。可选传入“参考蓝本”（库内某份合同全文），按其结构与行文风格起草。
本地 LLM 生成（Ollama），支持 rewrite 模式换一种结构与措辞重写。
"""
from agent.contract_kb import stream_generate

_DRAFT_PROMPT = (
    "你是专业的「合同起草智能体」。请根据用户的起草需求，生成一份结构完整、表述专业的中文合同草案。\n"
    "要求：\n"
    "1. 输出合同正文本身（不要解释、不要前后缀说明）；\n"
    "2. 采用规范条款式结构：合同名称、合同编号（如有）、甲方/乙方（用户未指定名称时用“甲方/乙方”占位，"
    "并在开头一句话说明“当事人名称可按实际填写”）、鉴于条款、合作内容/合同标的、价款与支付、"
    "履行期限、双方权利义务、保密、违约责任、合同变更与解除、争议解决、其他约定（生效、份数、签署）。\n"
    "3. 用户需求中明确提到的金额、期限、比例、地点、管辖等必须原样落实到对应条款；未提及的细节"
    "采用常见的合理默认（如争议先协商、协商不成向甲方所在地法院起诉），并尽量少做假设；\n"
    "4. 违约责任条款要写明违约情形、违约金或赔偿方式、补救措施；\n"
    "5. 用词严谨、条理清晰，便于直接使用。\n"
)

_REF_SEG = (
    "\n【参考蓝本合同（请参考其条款结构与行文风格起草，内容按新需求）】\n{ref}\n"
)

_MODE_TAIL = {
    "first": "",
    "rewrite": "\n\n额外要求：这是对同一需求的再次起草——请换一种与前版明显不同的条款组织与措辞，"
               "但覆盖的关键事项保持一致、质量相当。\n",
}


def draft(requirement: str, ref_text: str = "", mode: str = "first", emit=None) -> str:
    """生成合同草案。mode: first 首次起草 / rewrite 换一种写法。
    返回完整草案文本（流式时经 emit 逐段回传）。"""
    requirement = (requirement or "").strip()
    prompt = _DRAFT_PROMPT
    if ref_text:
        prompt += _REF_SEG.format(ref=ref_text)
    prompt += _MODE_TAIL.get(mode, "")
    prompt += "\n【起草需求】\n" + requirement
    return stream_generate(prompt, echo=False, emit=emit)
