#!/usr/bin/env python3
"""Check the deployed layerwise APIs, deterministic requests and TP receipts."""

import argparse
import json
import re
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def audit_receipts(args, results):
    deadline = time.monotonic() + 120
    while True:
        lines = []
        for log in args.logs.glob("d*/logs/vllm_decode_dp*.log"):
            lines.extend(
                (str(log), line)
                for line in log.read_text(errors="replace").splitlines()
            )
        covered = set()
        pending = []
        for result in results:
            receipts = [
                (path, line)
                for path, line in lines
                if result["id"] in line and "published after prepare_decode" in line
            ]
            ranks = {
                int(match[1])
                for _, line in receipts
                if (match := re.search(r"_TP(\d+)_EP", line))
            }
            retired = any(
                result["id"] in line and "Released target for" in line
                for _, line in lines
            )
            if ranks != set(range(args.tp)) or not retired:
                pending.append(
                    {"id": result["id"], "ranks": sorted(ranks), "retired": retired}
                )
            covered.update(path for path, _ in receipts)
        if not pending:
            assert len(covered) == args.expected_decode_engines, covered
            print(
                json.dumps(
                    {
                        "passed": len(results),
                        "decode_engines": sorted(covered),
                        "tp_receipts": args.tp,
                    }
                ),
                flush=True,
            )
            return
        if time.monotonic() >= deadline:
            raise AssertionError(pending)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="http://172.168.4.237:8333")
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--tp", default=8, type=int)
    parser.add_argument("--expected-decode-engines", default=4, type=int)
    parser.add_argument("--audit-results", type=Path)
    args = parser.parse_args()
    if args.audit_results:
        records = [
            json.loads(line) for line in args.audit_results.read_text().splitlines()
        ]
        # Support both smoke JSONL (including its summary) and long probe phases.
        results = [
            r for r in records if r.get("id") and r.get("phase") in (None, "complete")
        ]
        if not results:
            raise ValueError("Audit input contains no completed request records")
        audit_receipts(args, results)
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    run_nonce = uuid.uuid4().hex

    def request(case):
        label, repetitions = case
        replay = label.startswith("replay-")
        nonce = run_nonce + label.removeprefix("replay-")
        expected = "QUARTZ" if replay else "MAPLE"
        prompt = (
            f"Document {nonce}.\nThe first reference word is MAPLE.\n"
            + "Mooncake stores KV blocks as objects.\n" * repetitions
            + "\nThe second reference word is QUARTZ.\n"
            + (
                "Reply only with the second reference word."
                if replay
                else "Reply only with the first reference word."
            )
        )
        body = {
            "model": "glm-52",
            "messages": [{"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0,
            "max_tokens": 32,
            "stream": False,
        }
        start = time.monotonic()
        req = urllib.request.Request(
            args.proxy + "/v1/chat/completions",
            json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=900) as response:
            data = json.load(response)
        choice = data["choices"][0]
        answer = choice["message"].get("content", "").strip()
        result = {
            "case": label,
            "id": data["id"],
            "answer": answer,
            "usage": data["usage"],
            "finish_reason": choice["finish_reason"],
            "seconds": round(time.monotonic() - start, 3),
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        # A terminal full stop does not change the retrieved reference word.
        assert re.fullmatch(rf"{expected}[.!。！]?", answer), result
        assert choice["finish_reason"] == "stop", result
        if repetitions > 2000:
            assert data["usage"]["prompt_tokens"] > 16384, result
        return result

    # Exercise every D engine, including deployments with different P/D DP.
    decode_engines = args.expected_decode_engines
    results = [request((f"short-{rank}", 60)) for rank in range(decode_engines)]
    multichunk = [(f"multichunk-{rank}", 4200) for rank in range(decode_engines)]
    with ThreadPoolExecutor(max_workers=decode_engines) as pool:
        results.extend(pool.map(request, multichunk))
    results.extend(
        request((f"replay-{label}", repetitions)) for label, repetitions in multichunk
    )
    audit_receipts(args, results)


if __name__ == "__main__":
    main()
