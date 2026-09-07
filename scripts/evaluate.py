from __future__ import annotations

import json
import tempfile
from pathlib import Path

from service_flow import SupportAgent, SupportStore


ROUTE_CASES = [
    ("蓝牙连不上", "knowledge"), ("耳机搜不到", "knowledge"), ("摄像头离线", "knowledge"),
    ("怎么看隐私模式", "knowledge"), ("单耳有杂音", "knowledge"), ("质保多久", "knowledge"),
    ("我要退款", "refund"), ("七天内退货", "refund"), ("这个不要了", "refund"),
    ("帮我退钱", "refund"), ("我要人工", "ticket"), ("创建售后工单", "ticket"),
    ("设备坏了", "ticket"), ("我要投诉", "ticket"), ("电池鼓包", "safety"),
    ("机器冒烟", "safety"), ("设备起火", "safety"), ("耳机发烫", "safety"),
    ("老板是谁", "unknown"), ("推荐一首歌", "unknown"), ("今天天气", "unknown"),
    ("帮我订机票", "unknown"), ("讲个笑话", "unknown"), ("你会写诗吗", "unknown"),
]


def make_agent(root: Path, attempts: int = 2):
    store = SupportStore(root / "eval.db")
    store.seed()
    return store, SupportAgent(store, max_attempts=attempts)


def reliability(attempts: int) -> tuple[float, int]:
    successes = 0
    duplicates = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store, agent = make_agent(root, attempts)
        for i in range(30):
            request_id = f"load-{attempts}-{i}"
            proposed = agent.handle(request_id, "C-001", "O-1001", "七天内申请退款")
            permanent = i == 29
            transient = i % 3 == 0
            reply = agent.confirm_refund(
                proposed.confirmation_token,
                "C-001",
                fail_policy=lambda attempt, transient=transient, permanent=permanent: permanent or (transient and attempt == 1),
            )
            successes += reply.status == "completed"
            if reply.status == "completed":
                before = store.effect_count()
                agent.confirm_refund(proposed.confirmation_token, "C-001")
                duplicates += store.effect_count() - before
        store.close()
    return successes / 30, duplicates


def main() -> None:
    correct = sum(SupportAgent.classify(text) == expected for text, expected in ROUTE_CASES)
    with tempfile.TemporaryDirectory() as tmp:
        store, agent = make_agent(Path(tmp))
        safety = ["电池鼓包", "产品冒烟", "设备起火", "耳机发烫"]
        safety_handoff = sum(agent.handle(f"safe-{i}", "C-001", "O-1001", q).status == "handoff" for i, q in enumerate(safety))
        unknown = ["老板是谁", "帮我订酒店", "今天星期几", "推荐一部电影", "给我讲故事", "查快递公司股价"]
        unknown_handoff = sum(agent.handle(f"unk-{i}", "C-001", "O-1001", q).status == "handoff" for i, q in enumerate(unknown))
        store.close()
    baseline, _ = reliability(1)
    retried, duplicates = reliability(2)
    result = {
        "dataset": {"route_cases": len(ROUTE_CASES), "fault_injection_tasks": 30, "safety_cases": 4, "unknown_cases": 6},
        "routing": {"accuracy": round(correct / len(ROUTE_CASES), 4)},
        "handoff": {"safety_coverage": round(safety_handoff / 4, 4), "unknown_query_coverage": round(unknown_handoff / 6, 4)},
        "reliability": {"single_attempt_success_rate": round(baseline, 4), "two_attempt_success_rate": round(retried, 4), "absolute_improvement_points": round((retried - baseline) * 100, 1)},
        "idempotency": {"duplicate_refund_effects_after_replay": duplicates},
        "scope": "本地合成订单、知识库与确定性故障注入，仅用于机制回归，不代表线上业务指标",
    }
    out = Path("evaluation")
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(
        "# ServiceFlow 固定集评测\n\n```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

