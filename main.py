# -*- coding: utf-8 -*-
"""
main.py —— 系统统一入口（答辩演示菜单）
把“入库 → 检索问答 → 合同审查 → 要素抽取 → 记忆 Agent”整条链路串成菜单，
按数字键即可完成全流程演示。
运行：python main.py
"""
import os

from config import CONTRACTS_DIR, LLM_MODEL_NAME, EMBED_MODEL_NAME
from contract_analyzer import _list_contract_files, analyze_contract, print_review_report
from contract_kb import llm, show_rag_demo
from element_extractor import extract_elements, print_elements
from vector_store import add_dir_to_kb, add_file_to_kb, clear_db, db_stats

BANNER = f"""
╔══════════════════════════════════════════════════════════╗
║   本地化智能合同审查系统  Contract AI (Ollama 离线版)       ║
║   LLM:{LLM_MODEL_NAME:<22} Embedding:{EMBED_MODEL_NAME:<22}║
╚══════════════════════════════════════════════════════════╝
"""

MENU = """
============== 功能菜单 ==============
 1. 导入合同入库        （批量扫描 contracts 目录 → Chroma）
 2. 知识库问答          （RAG：基于合同原文作答，附引用出处）
 3. 合同智能审查        （8 维度风险分析，输出审查报告）
 4. 关键要素抽取        （结构化提取 当事人/金额/期限/日期等）
 5. Agent 多轮对话      （LangGraph 记忆智能体，自动调用工具）
 6. 查看向量库状态
 7. 清空向量库
 0. 退出
======================================
"""


def _pick_contract(prompt: str) -> str:
    """让用户从 contracts 目录选择一份合同，返回文件名。"""
    files = _list_contract_files()
    if not files:
        print(f"❗ contracts 目录为空，请先放入 txt/pdf 合同文件（{os.path.abspath(CONTRACTS_DIR)}）")
        return ""
    print("\n可用合同：")
    for i, f in enumerate(files, 1):
        print(f"  {i}. {f}")
    try:
        idx = int(input(f"\n{prompt}（输入序号）: ")) - 1
        return files[idx]
    except (ValueError, IndexError):
        print("输入无效")
        return ""


def main() -> None:
    print(BANNER)
    # 启动自检：验证本地模型连通
    print("⏳ 正在连接本地 Ollama 服务 ...")
    try:
        llm.invoke("你好")
        print("✅ 大模型连接正常\n")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  大模型连接失败：{e}")
        print("   请确认 Ollama 已启动，并已拉取模型（见 README）")
        return

    while True:
        print(MENU)
        choice = input("请选择功能: ").strip()
        print()

        if choice == "1":
            print("选择导入方式：1=单文件  2=整目录批量")
            mode = input("请选择: ").strip()
            if mode == "1":
                name = _pick_contract("选择要导入的合同")
                if name:
                    add_file_to_kb(os.path.join(CONTRACTS_DIR, name))
            else:
                add_dir_to_kb(CONTRACTS_DIR)

        elif choice == "2":
            q = input("请输入合同相关问题（回车返回菜单）: ").strip()
            if q:
                show_rag_demo(q)

        elif choice == "3":
            name = _pick_contract("选择要审查的合同")
            if name:
                print(f"\n⏳ 正在对《{name}》执行多维度审查（约需 1-2 分钟）...\n")
                report = analyze_contract(name)
                print_review_report(name, report)

        elif choice == "4":
            name = _pick_contract("选择要抽取要素的合同")
            if name:
                print("\n⏳ 正在抽取关键要素 ...\n")
                data = extract_elements(name)
                print_elements(name, data)

        elif choice == "5":
            print("进入 Agent 多轮对话（输入 exit 返回菜单）\n")
            from agent_run import interactive

            interactive(thread_id="demo_session")

        elif choice == "6":
            stats = db_stats()
            print(f"📊 向量库状态：共 {stats['total_chunks']} 个切片")
            print(f"   涉及文件：{', '.join(stats['files']) if stats['files'] else '（空）'}")

        elif choice == "7":
            confirm = input("确认清空整个向量库？(y/N): ").strip().lower()
            if confirm == "y":
                n = clear_db()
                print(f"已清空 {n} 条切片")

        elif choice == "0":
            print("已退出系统 👋")
            break

        else:
            print("无效输入，请重新选择")


if __name__ == "__main__":
    main()
