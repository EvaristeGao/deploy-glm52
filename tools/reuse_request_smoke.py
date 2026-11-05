#!/usr/bin/env python3
"""Run P/D smoke requests from the jump host without writing remote host files."""

import subprocess

from reuse_container_experiment import assert_development_cluster

REMOTE = r"""
import json, time, urllib.request, uuid

def post(path, body, timeout=300):
    request = urllib.request.Request("http://172.168.4.237:8000" + path,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)

for repeats in (60, 620, 1200):
    nonce = uuid.uuid4().hex
    content = ("任务编号 " + nonce + "。请阅读资料后回答最后的问题。\n"
        + "Mooncake stores KV blocks as objects. " * repeats
        + "\n请只用一句中文回答：以上资料主要描述 Mooncake 如何保存什么？")
    body = {"model": "glm-52", "messages": [{"role": "user", "content": content}],
            "max_tokens": 128, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    started = time.monotonic()
    try:
        result = post("/v1/chat/completions", body)
        message = result["choices"][0]["message"]
        output = (message.get("content") or "") + (message.get("reasoning") or "")
        print(json.dumps({"repeats": repeats, "seconds": time.monotonic()-started,
            "usage": result.get("usage"), "message": message,
            "has_kv": "KV" in output.upper(), "finish_reason": result["choices"][0]["finish_reason"]},
            ensure_ascii=False), flush=True)
        if not output.strip():
            raise RuntimeError("Empty output")
    except Exception as exc:
        print(json.dumps({"repeats": repeats, "error": repr(exc),
            "seconds": time.monotonic()-started}), flush=True)
        raise
"""


if __name__ == "__main__":
    assert_development_cluster()
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
        input=REMOTE.encode(),
        check=True,
    )
