# agent —— 业务层。讲代码时的入口顺序：
#   contract_kb / kb_agent          问答
#   contract_analyzer               8 维审查
#   element_extractor               要素抽取（巡检复用）
#   intake_agent                    网页入库的解析编排
#   tools.py + agent_run.py         10 个工具与 LangGraph
#   jobs.py + app.py                长任务与 FastAPI
#   draft_agent.py + draft_export   起草与导出
#   report_agent.py                 审查结果摘要
#   main.py                         命令行菜单（不走登录）

