#!/usr/bin/env python3
"""Audit recorded P-only request results; never submit or restart anything."""

import json
from pathlib import Path

from reuse_container_experiment import ART
from reuse_long_context_probe import read_archive


def main():
    root = ART / "one-million"
    cases = [
        ("ALPHA", target, root / f"chunk32784-v2/alpha-{target}.log")
        for target in (8192, 65536, 131072, 262144, 524288, 1048544)
    ] + [
        (label, 1048544, root / f"prefix-replay/{label.lower()}-1048544.log")
        for label in ("BETA", "GAMMA")
    ]
    passed, pending, failed, archives = [], [], [], {}
    for label, target, path in cases:
        name = str(path.relative_to(root))
        if not path.exists():
            pending.append(name)
            continue
        events = []
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if any(event.get("event") == "error" for event in events):
            failed.append({"file": name, "error": "Recorded request error"})
            continue
        complete = [event for event in events if event.get("event") == "complete"]
        if not complete:
            pending.append(name)
            continue
        try:
            archive = read_archive(path, target)
            result = complete[0]
            if archive["query_label"] != label:
                raise ValueError("Wrong retrieval position")
            if (
                result["target"] != target
                or abs(result["usage"]["prompt_tokens"] - target) > 16
            ):
                raise ValueError("Actual token count does not match the target")
            if target == 1048544 and result["usage"]["prompt_tokens"] < 1_000_000:
                raise ValueError(
                    "The recorded request is shorter than one million tokens"
                )
            if result["usage"]["prompt_tokens"] + 1 > 1048576:
                raise ValueError("Request exceeds the configured context limit")
            archives[label, target] = archive
            passed.append(
                {
                    "file": name,
                    "label": label,
                    "input_tokens": result["usage"]["prompt_tokens"],
                    "seconds": result["seconds"],
                    "answer": result["message"]["content"],
                }
            )
        except (ValueError, KeyError, TypeError) as exc:
            failed.append({"file": name, "error": str(exc)})
    source = archives.get(("ALPHA", 1048544))
    if source:
        if source.get("archive_source"):
            failed.append(
                {"error": "The first full 1M request must use a fresh archive"}
            )
        for label in ("BETA", "GAMMA"):
            archive = archives.get((label, 1048544))
            if archive is None:
                continue
            if (
                archive["nonce"] != source["nonce"]
                or archive["expected_words"] != source["expected_words"]
            ):
                failed.append({"error": f"{label} did not preserve the source archive"})
            if (
                Path(archive.get("archive_source") or "").resolve()
                != cases[5][2].resolve()
            ):
                failed.append(
                    {
                        "error": f"{label} does not identify the first completed 1M archive"
                    }
                )
    cache_observation = None
    before_path = root / "prefix-replay/before.json"
    after_path = root / "prefix-replay/after-gamma.json"
    if before_path.exists() and after_path.exists():
        before, after = (
            json.loads(before_path.read_text()),
            json.loads(after_path.read_text()),
        )

        def total(values, prefix):
            if not isinstance(values, list):
                raise TypeError("Metrics snapshot unavailable")
            matches = [
                float(line.rsplit(" ", 1)[1])
                for line in values
                if line.startswith(prefix + "{")
            ]
            if not matches:
                raise ValueError(f"Metric missing: {prefix}")
            return sum(matches)

        try:
            keys = (
                "vllm:prefix_cache_hits_total",
                "vllm:external_prefix_cache_hits_total",
            )
            cache_observation = {
                key: {
                    "before": total(before["p0"], key),
                    "after": total(after["p0"], key),
                }
                for key in keys
            }
            if not any(
                value["after"] > value["before"] for value in cache_observation.values()
            ):
                failed.append(
                    {"error": "No actual prefix hit observed for the replay cases"}
                )
            for snapshot in (before, after):
                for node in ("d0", "d1"):
                    if (
                        total(snapshot[node], "vllm:request_success_total") != 0
                        or total(snapshot[node], "vllm:num_requests_running") != 0
                    ):
                        failed.append({"error": f"Unexpected Decode traffic: {node}"})
        except (ValueError, KeyError, TypeError) as exc:
            failed.append({"error": str(exc)})
    else:
        pending.append("prefix replay metrics snapshots")
    report = {
        "request_checks_passed": not pending and not failed,
        "passed": passed,
        "pending": pending,
        "failed": failed,
        "cache_observation": cache_observation,
        "remaining_runtime_audit": "Check post-run fresh requests, health, current-run errors, source hashes and held-controller cleanup separately",
    }
    (root / "result-audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failed else 2 if pending else 0


if __name__ == "__main__":
    raise SystemExit(main())
