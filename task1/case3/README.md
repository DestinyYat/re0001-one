赛题二 · 10 月 16 日开发总结
今日核心成果：case3 本地工程流程跑通

扫描链总数
70

DRC 违规
0

要求的输出文件
9/9


EDA 退出码 0；运行次数 1；尚未经过官方隐藏评测。

一、今天完成的工作
1. 搭建并验证 LangGraph 智能体框架
   建立了 initialize → generate → run_tool → validate → diagnose/repair → export 的工作流程。case1、case2 此前已有基础流程成功记录，但当时主要依靠 Python 规则，并未真正使用 LLM 生成决策。
2. 打通 DeepSeek API，定位模型生成问题
   确认 deepseek-v4-pro 和 deepseek-flash 可调用。排查了请求超时和 reasoning_tokens 耗尽的问题：复杂 Prompt 可能消耗全部输出预算，无法返回 Tcl 或 JSON；小型 JSON 提取实验则成功。
3. 完成 V3.2 混合式 Agent
   采用 LLM 解析设计信息、Python 提取明确约束、确定性编译 Dofile 的方案。保留 LangGraph 工具执行、检查和潜在的 LLM 自动修复流程。
4. 成功运行 case3
   实际调用 dftexp_scan，完成 DRC、扫描链插入、报告生成、Verilog/CTL/SCANDEF 导出，并检查了关键配置及日志。
二、三个 Case 的当前状态
Case	进展	仍需验证
case1	基础版本已有成功记录	V3.2/V4 统一代码回归
case2	基础版本已有成功记录	V3.2/V4 统一代码回归
case3	本地工程验证通过	官方评测、进一步语义校验
case3 的关键参数均已核对：55 条普通扫描链、15 条 dedicated Wrapper 链，两时钟、允许混合时钟、DRC violations 为 0。61 条被工具检查的链全部连接一致；SCAN-4902 属于赛题说明允许的警告类型。
三、目前还存在哪些问题？
最大的问题已经不是 EDA 工具能否运行，而是智能体的泛化与自主能力。
- 仍存在硬编码：输出文件名 e902_* 以及部分报告规则仍针对 case3。
- LLM 参与程度有限：最后一次成功执行复用了之前模型生成的 Design 配置，Scan 和 Blackbox 使用规则解析，因此该次 llm_calls=0。
- 修复能力尚未验证：虽然有 diagnose/repair 节点，但还没有完整跑通故障诊断、修复、重新执行的闭环。
- 验证器需要增强：目前主要依据退出码、文件存在性等检查，还需要自动核对扫描链、DRC 和各类约束。

四、明天从哪里继续？
下一阶段：V4 通用 LLM Agent

1. 保存 case3 成功版本和运行日志，建立稳定基线。
2. 去掉 case3 硬编码，实现输入驱动的 Dofile 编译。
3. 使用同一套代码回归 case1、case2、case3。
4. 设计可恢复故障测试，验证真正的 LLM 自动修复。
5. 完善审计日志和最终提交入口。

当前工程位置
/case/output
  ├── main.py
  ├── test.py
  ├── generated.dofile
  ├── agent_report.json
  ├── scan_run_1.log
  ├── config_attempt_1.json
  └── reports/