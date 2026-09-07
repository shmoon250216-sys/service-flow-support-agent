# ServiceFlow｜智能售后工单 Agent

GitHub：<https://github.com/shmoon250216-sys/service-flow-support-agent>  
作者主页：<https://github.com/shmoon250216-sys>

面向消费电子售后的个人项目。它把“知识库问答、订单校验、危险问题转人工、退款确认、工具失败恢复”串成一条可运行链路，避免客服机器人只会聊天、不能安全执行业务动作。

## 解决的问题

- 用户说法口语化：用同义词扩展和知识检索返回带来源的排障步骤。
- 低证据与安全风险：未知问题转人工；冒烟、鼓包、过热等问题直接进入紧急专席，不继续自助排障。
- 越权与误操作：售后动作先校验订单归属；退款只生成提案，必须使用确认令牌后才提交。
- 超时与重复提交：事件日志保存每次尝试；失败状态可用同一令牌恢复；SHA-256 幂等键阻止重复退款。

## 运行

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
python scripts/evaluate.py
uvicorn service_flow.api:app --reload
```

主要接口：`POST /chat` 和 `POST /refund/confirm`。演示数据为本地合成订单及知识库，不含真实用户信息。

## 固定评测

`scripts/evaluate.py` 会生成 `evaluation/results.json`，覆盖 24 条路由样例、10 条人工转接样例和 30 个确定性故障注入任务。指标仅说明当前代码在固定回归集上的表现。

## 设计参考

- [AgentDesk](https://github.com/huabeitech/agent-desk)：知识约束、Answerability Gate、人工转接和工单闭环。
- [Google Cloud Cymbal Air Toolbox Demo](https://github.com/GoogleCloudPlatform/cymbal-air-toolbox-demo)：RAG、数据库与客服工具协同的应用场景。
- [Agentic Customer Service Platform](https://github.com/negativexq/agentic-customer-service-platform)：确认门、策略校验与幂等副作用的生产约束。

本项目只借鉴公开架构思路，代码、合成数据和评测脚本均在本地重新实现。
