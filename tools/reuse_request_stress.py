#!/usr/bin/env python3
"""Concurrent prefix contention and cancellation probes on the development proxy."""

import subprocess

from reuse_container_experiment import assert_development_cluster

REMOTE = r"""
from concurrent.futures import ThreadPoolExecutor
import http.client, json, time, urllib.request, uuid

base = "请求组 " + uuid.uuid4().hex + "\n" + "Mooncake stores KV blocks as objects. " * 620

def body(content, stream=False):
    return {"model":"glm-52", "messages":[{"role":"user", "content":content}],
            "max_tokens":128, "temperature":0, "stream":stream,
            "chat_template_kwargs":{"enable_thinking":False}}

def ask(label, content):
    started=time.monotonic()
    request=urllib.request.Request("http://172.168.4.237:8000/v1/chat/completions",
        data=json.dumps(body(content)).encode(), headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        result=json.load(response)
    message=result["choices"][0]["message"]
    output=(message.get("content") or "")+(message.get("reasoning") or "")
    print(json.dumps({"case":label,"seconds":time.monotonic()-started,
        "usage":result.get("usage"),"message":message},ensure_ascii=False),flush=True)
    if "KV" not in output.upper():
        raise AssertionError("Response did not describe KV storage")

suffix="\n请只用一句中文概括以上资料。"
with ThreadPoolExecutor(max_workers=2) as pool:
    futures=[pool.submit(ask, "concurrent-"+str(i), base+suffix+"编号"+str(i)) for i in range(2)]
    for future in futures:
        future.result()
ask("prefix-repeat",base+suffix+"编号0")

# Cancel this probe's own connection while Prefill may still be active.
connection=http.client.HTTPConnection("172.168.4.237",8000,timeout=10)
connection.request("POST","/v1/chat/completions",
    body=json.dumps(body("取消探针 "+uuid.uuid4().hex+"\n"+base*2+suffix,True)),
    headers={"Content-Type":"application/json"})
time.sleep(0.5)
connection.close()
print(json.dumps({"case":"cancel-own-connection","result":"connection closed"}),flush=True)
time.sleep(2)
ask("after-cancellation", "独立请求 "+uuid.uuid4().hex+"\nMooncake stores KV blocks as objects."+suffix)
"""


if __name__ == "__main__":
    assert_development_cluster()
    subprocess.run(
        ["ssh", "root@ott9-a31", "python3", "-u", "-"],
        input=REMOTE.encode(),
        check=True,
    )
