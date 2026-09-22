"""Execute labelled multi-turn fixtures against real domain services; no LLM judge."""

from __future__ import annotations
import hashlib
import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from service_flow.store import SupportStore
from service_flow.conversations import ConversationService

ROOT = Path(__file__).resolve().parents[1]


def run(dataset=None, output=None):
    dataset = Path(dataset or ROOT / "evaluation/datasets/support-v1.jsonl")
    rows = [
        json.loads(x)
        for x in dataset.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    cases = []
    by_category = defaultdict(lambda: {"passed": 0, "total": 0})
    for fixture in rows:
        started = time.perf_counter()
        failures = []
        with tempfile.TemporaryDirectory() as tmp:
            store = SupportStore(Path(tmp) / "case.db")
            store.seed()
            try:
                service = ConversationService(store)
                cid = service.create(fixture["customer_id"], fixture["order_id"])["id"]
                for number, turn in enumerate(fixture["turns"]):
                    reply = service.send(
                        cid,
                        fixture["customer_id"],
                        f"{fixture['id']}-{number}",
                        turn["message"],
                    )
                    actual = {
                        **reply,
                        "article_id": reply.get("citations", [{}])[0].get("article_id")
                        if reply.get("citations")
                        else None,
                    }
                    for key, expected in turn["expect"].items():
                        if actual.get(key) != expected:
                            failures.append(
                                {
                                    "turn": number,
                                    "field": key,
                                    "expected": expected,
                                    "actual": actual.get(key),
                                }
                            )
                effects = store.connection.execute(
                    "SELECT count(*) FROM refund_ledger"
                ).fetchone()[0]
                if effects > fixture["max_refund_effects"]:
                    failures.append(
                        {"field": "unconfirmed_side_effects", "actual": effects}
                    )
            except Exception as exc:
                failures.append({"exception": type(exc).__name__, "message": str(exc)})
            finally:
                store.close()
        passed = not failures
        group = by_category[fixture["category"]]
        group["total"] += 1
        group["passed"] += passed
        cases.append(
            {
                "id": fixture["id"],
                "category": fixture["category"],
                "passed": passed,
                "failures": failures,
                "local_duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    result = {
        "dataset": dataset.name,
        "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "cases": len(cases),
        "turns": sum(len(x["turns"]) for x in rows),
        "passed": sum(x["passed"] for x in cases),
        "by_category": dict(by_category),
        "scope": "开发者编写的合成回归集；非独立测试集、非真实模型评测，不代表线上准确率",
        "results": cases,
    }
    destination = Path(output or ROOT / "evaluation/scenario-results.json")
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "results"},
            ensure_ascii=False,
            indent=2,
        )
    )
    for case in result["results"]:
        if not case["passed"]:
            print(json.dumps(case, ensure_ascii=False))
    sys.exit(0 if result["passed"] == result["cases"] else 1)
