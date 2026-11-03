#!/usr/bin/env python3
"""Measure actual tokens and test old-context retrieval through the dev cluster."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from reuse_container_experiment import assert_development_cluster

REMOTE = r"""
import hashlib, json, random, time, urllib.error, urllib.request, uuid

def post(url, body, timeout):
    request=urllib.request.Request(url,data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail=exc.read(8192).decode(errors="replace")
        raise RuntimeError("HTTP "+str(exc.code)+" from "+url+": "+detail) from exc

words="maple coral silver violet cedar amber tulip olive copper willow quartz lemon velvet lotus apricot marble".split()
filler="Mooncake stores KV blocks as objects. "
for target in config["tokens"]:
    archive=config.get("archive")
    nonce=archive["nonce"] if archive else uuid.uuid4().hex
    answers=archive["expected_words"] if archive else random.SystemRandom().sample(list("123456789") if config["prefill_only"] else words,3)
    repeats=max(1,(target-160)//8)
    def messages(n, query_label=None):
        cuts=[n//10,n//2,n*9//10]
        parts=["Independent archive "+nonce+". Remember the three verification words.\n"]
        previous=0
        for label,word,cut in zip(("ALPHA","BETA","GAMMA"),answers,cuts):
            parts.extend([filler*(cut-previous),"\nVerification word for "+label+" is "+word+".\n"])
            previous=cut
        question=("\nReturn ONLY the single digit assigned to "+(query_label or config["query_label"])+". No explanation, spaces, or punctuation.") if config["prefill_only"] else "\nReturn only the three verification words in ALPHA BETA GAMMA order; no explanation."
        parts.extend([filler*(n-previous),question])
        return [{"role":"user","content":"".join(parts)}]
    for attempt in range(5):
        used_repeats=repeats
        msgs=messages(repeats, archive["query_label"] if archive else None)
        tokenized=post(config["tokenizer"]+"/tokenize",{
            "model":"glm-52","messages":msgs,"add_generation_prompt":True,
            "chat_template_kwargs":{"enable_thinking":False}},120)
        count=tokenized["count"]
        if abs(count-target)<=8:
            break
        delta=target-count
        repeats=max(1,repeats+(delta//8 if abs(delta)>=8 else (1 if delta>0 else -1)))
    if archive:
        if count!=archive["measured_prompt_tokens"] or hashlib.sha256(msgs[0]["content"].encode()).hexdigest()!=archive["prompt_sha256"]:
            raise RuntimeError("Archive replay differs from the original prompt; refusing a claimed prefix-reuse test")
        msgs=messages(used_repeats)
        tokenized=post(config["tokenizer"]+"/tokenize",{
            "model":"glm-52","messages":msgs,"add_generation_prompt":True,
            "chat_template_kwargs":{"enable_thinking":False}},120)
        count=tokenized["count"]
    if abs(count-target)>16 or count+config["max_tokens"]>1048576:
        raise RuntimeError("Unable to calibrate a safe prompt length: "+str(count))
    print(json.dumps({"event":"start","target":target,"measured_prompt_tokens":count,
        "prompt_sha256":hashlib.sha256(msgs[0]["content"].encode()).hexdigest(),
        "expected_words":answers,"nonce":nonce,"endpoint":config["endpoint"],
        "prefill_only":config["prefill_only"],"query_label":config["query_label"],
        "filler_repeats":used_repeats,"archive_source":config.get("archive_source"),
        "started_at":time.time()}),flush=True)
    if config.get("calibrate_only"):
        print(json.dumps({"event":"calibration_complete","target":target,
            "archive_replay_verified":bool(archive),"measured_prompt_tokens":count}),flush=True)
        continue
    started=time.monotonic()
    try:
        result=post(config["endpoint"]+"/v1/chat/completions",{
            "model":"glm-52","messages":msgs,"temperature":0,
            "max_tokens":config["max_tokens"],
            "chat_template_kwargs":{"enable_thinking":False}},config["timeout"])
        message=result["choices"][0]["message"]
        text=(message.get("content") or "")+(message.get("reasoning") or "")
        actual=result.get("usage",{}).get("prompt_tokens")
        expected=answers[("ALPHA","BETA","GAMMA").index(config["query_label"])] if config["prefill_only"] else None
        passed=text.strip()==expected if config["prefill_only"] else all(word in text.lower() for word in answers)
        print(json.dumps({"event":"complete","target":target,"seconds":time.monotonic()-started,
            "measured_prompt_tokens":count,"usage":result.get("usage"),"message":message,
            "retrieval_pass":passed,"expected_digit":expected,"finish_reason":result["choices"][0]["finish_reason"]},
            ensure_ascii=False),flush=True)
        if actual is None or abs(actual-count)>16:
            raise AssertionError("Actual input token count differs from tokenizer calibration")
        if not passed:
            raise AssertionError("Retrieval check failed; stop before increasing context")
    except Exception as exc:
        print(json.dumps({"event":"error","target":target,"seconds":time.monotonic()-started,
            "error":repr(exc)},ensure_ascii=False),flush=True)
        raise
"""


def read_archive(path, target):
    events = [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.startswith("{")
    ]
    starts = [event for event in events if event.get("event") == "start"]
    completes = [event for event in events if event.get("event") == "complete"]
    if (
        len(starts) != 1
        or len(completes) != 1
        or any(event.get("event") == "error" for event in events)
    ):
        raise ValueError("Archive must contain one completed successful request")
    start, complete = starts[0], completes[0]
    answers = start.get("expected_words", [])
    if not (
        start.get("target") == target
        and start.get("prefill_only") is True
        and start.get("query_label") in ("ALPHA", "BETA", "GAMMA")
        and re.fullmatch(r"[0-9a-f]{32}", start.get("nonce", ""))
        and len(answers) == len(set(answers)) == 3
        and all(answer in "123456789" and len(answer) == 1 for answer in answers)
        and complete.get("retrieval_pass") is True
        and complete.get("usage", {}).get("completion_tokens") == 1
        and complete.get("usage", {}).get("prompt_tokens")
        == start.get("measured_prompt_tokens")
        and complete.get("message", {}).get("content", "").strip()
        == answers[("ALPHA", "BETA", "GAMMA").index(start["query_label"])]
    ):
        raise ValueError(
            "Archive does not prove a successful calibrated P-only request"
        )
    return start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokens", type=int, nargs="+", default=[8192])
    parser.add_argument("--target", choices=["pd", "p0", "p1"], default="pd")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument(
        "--prefill-only",
        action="store_true",
        help="Direct P request with exactly one generated digit",
    )
    parser.add_argument(
        "--query-label", choices=["ALPHA", "BETA", "GAMMA"], default="ALPHA"
    )
    parser.add_argument(
        "--reuse-prefix-from",
        type=Path,
        help="Explicitly reuse the archive of a completed P-only probe; only change the final question",
    )
    parser.add_argument(
        "--calibrate-only",
        action="store_true",
        help="Check tokenization and archive replay without running model inference",
    )
    args = parser.parse_args()
    if args.prefill_only:
        if args.target == "pd":
            parser.error("--prefill-only requires --target p0 or p1")
        args.max_tokens = 1
    if any(
        value < 512 or value + args.max_tokens + 16 > 1048576 for value in args.tokens
    ):
        parser.error(
            "Keep input plus generation and calibration margin within 1048576 tokens"
        )
    if args.timeout < 1 or args.max_tokens < 1:
        parser.error("Timeout and generation limit must be positive")
    archive = None
    if args.reuse_prefix_from:
        if not args.prefill_only or len(args.tokens) != 1:
            parser.error("Prefix replay requires --prefill-only and one token target")
        archive = read_archive(args.reuse_prefix_from, args.tokens[0])
    if args.prefill_only:
        from reuse_container_experiment import baseline, inspect, owned

        if inspect(args.target, baseline(args.target)["Name"])["State"]["Running"]:
            raise RuntimeError("Baseline is running; refusing a development probe")
        if not owned(args.target)["State"]["Running"]:
            raise RuntimeError("Selected Prefill development container is stopped")
    else:
        assert_development_cluster()
    endpoints = {
        "pd": "http://172.168.4.237:8000",
        "p0": "http://172.168.4.237:9081",
        "p1": "http://172.168.4.31:9081",
    }
    config = {
        "tokens": args.tokens,
        "endpoint": endpoints[args.target],
        "tokenizer": "http://172.168.4.237:9081",
        "timeout": args.timeout,
        "max_tokens": args.max_tokens,
        "prefill_only": args.prefill_only,
        "query_label": args.query_label,
        "archive": archive,
        "archive_source": str(args.reuse_prefix_from) if archive else None,
        "calibrate_only": args.calibrate_only,
    }
    code = "config=" + repr(config) + "\n" + REMOTE
    subprocess.run(
        [
            "ssh",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "ServerAliveInterval=30",
            "root@ott9-a31",
            "python3",
            "-u",
            "-",
        ],
        input=code.encode(),
        check=True,
    )


if __name__ == "__main__":
    main()
