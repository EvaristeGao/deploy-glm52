#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟各部署模式的 playbook 渲染，逐命令检查（无实机、不依赖 /dev/shm）。

用法：
    .venv/bin/python simulate_modes.py            # 全模式
    .venv/bin/python simulate_modes.py standalone  # 只跑某模式（关键字过滤）

原理：用 jinja2（trim_blocks=True，与 Ansible 模板一致）复刻 start.yml / gen.yml 的
    when 判定、set_fact 派生、shell/template/copy 渲染；每个 shell 命令跑 bash -n 语法检查。
    不连节点、不跑 docker、不占卡。local_ip 等不再用运行时命令（已统一 inventory ip 字段）。
"""
import os
import subprocess
import sys
import tempfile
import yaml
from jinja2 import Environment, FileSystemLoader, Undefined

BASE = os.path.dirname(os.path.abspath(__file__))
INV = yaml.safe_load(open(os.path.join(BASE, "inventories/a2/inventory.yaml")))
GV = yaml.safe_load(open(os.path.join(BASE, "inventories/a2/group_vars/all.yml")))

# ---- inventory 解析：groups / hostvars ----
groups = {"all": []}
hostvars = {}
for child, cdef in INV["all"]["children"].items():
    hosts = list(cdef.get("hosts", {}).keys())
    groups[child] = hosts
    groups["all"].extend(hosts)
    for name, v in cdef.get("hosts", {}).items():
        if v:
            hostvars.setdefault(name, {}).update(v)
groups["all"] = sorted(set(groups["all"]))

class ChainUndefined(Undefined):
    """AnsibleUndefined 近似：可链式访问、假值、可迭代空、字符串化空串。
    （真实 Ansible 里 skipped 任务 register 的变量为 AnsibleUndefined，when 里访问不报错）"""
    def __getattr__(self, name):
        return ChainUndefined(hint=f"chain.{name}")
    def __getitem__(self, key):
        return ChainUndefined(hint=f"chain[{key!r}]")
    def __iter__(self):
        return iter(())
    def __len__(self):
        return 0
    def __bool__(self):
        return False
    def __eq__(self, other):
        return other is None
    def __ne__(self, other):
        return other is not None
    def __contains__(self, item):
        return False
    def __str__(self):
        return ""


env = Environment(loader=FileSystemLoader(os.path.join(BASE, "templates")),
                  undefined=ChainUndefined, trim_blocks=True)
# Ansible 自定义 filter（纯 jinja2 没有）
import itertools
env.filters["dirname"] = os.path.dirname
env.filters["basename"] = os.path.basename
env.filters["product"] = lambda *a: list(itertools.product(*a))

MODES = [
    ("HA_per_container", dict(enabled=True, enable_ha=True, haproxy_mode="per_container"), "kvpool HA · 内嵌 haproxy（默认）"),
    ("HA_standalone", dict(enabled=True, enable_ha=True, haproxy_mode="standalone"), "kvpool HA · 独立 haproxy"),
    ("kvpool_single", dict(enabled=True, enable_ha=False, haproxy_mode="per_container"), "kvpool 单主"),
    ("no_kvpool", dict(enabled=False, enable_ha=False, haproxy_mode="per_container"), "无 kvpool"),
]


def build_base(mode_cfg):
    """构造模式覆盖后的基础 ctx（group_vars 覆盖 + 魔法变量）。"""
    mc = dict(GV["mooncake"])
    mc["enabled"] = mode_cfg["enabled"]
    mc["ha"] = dict(GV["mooncake"]["ha"], enable_ha=mode_cfg["enable_ha"])
    base = dict(GV)
    base["mooncake"] = mc
    base["haproxy"] = dict(GV["haproxy"], mode=mode_cfg["haproxy_mode"])
    base["groups"] = groups
    base["hostvars"] = hostvars
    return base


def ev(expr, ctx):
    return env.compile_expression(str(expr))(**ctx)


def val(v, ctx):
    """变量值：恰为单个 {{ expr }}（恰好 1 组定界符、无 {% %}）→ 求值为原生类型；
    其余（混排多组 {{ }} / 含 {% %} / 纯文本）按模板渲染成字符串。"""
    v = str(v)
    s = v.strip()
    if s.startswith("{{") and s.endswith("}}") and s.count("{{") == 1 and s.count("}}") == 1 and "{%" not in s:
        return ev(s[2:-2].strip(), ctx)
    return rt(v, ctx)


def loop_items(t, ctx):
    """loop: 若是 {{ }} 表达式则求值为列表；若是 YAML 列表直接返回。"""
    l = t["loop"]
    if isinstance(l, str) and "{{" in l:
        return ev(l.replace("{{", "").replace("}}", ""), ctx)
    return l


def rt(tpl, ctx):
    return env.from_string(str(tpl)).render(**ctx)


def bash_ok(cmd):
    f = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    f.write(cmd)
    f.close()
    try:
        r = subprocess.run(["bash", "-n", f.name], capture_output=True)
        return r.returncode == 0
    finally:
        os.unlink(f.name)


def tpl_markers(out):
    """提取引擎模板渲染的关键行（kv-transfer / MOONCAKE / local_ip / serve 参数）。"""
    keys = []
    for ln in out.splitlines():
        s = ln.strip()
        if any(k in s for k in (
                "local_ip=", "MOONCAKE_CONFIG_PATH", "--kv-transfer-config", '"kv_connector"',
                "MultiConnector", "AscendStoreConnector", "MooncakeConnectorV1", "engine_id",
                "use_ascend_direct", '"kv_role"', '"kv_port"', "vllm serve",
                "--data-parallel-address", "--max-num-seqs", "--kv-port", "--speculative-config")):
            keys.append(s)
    return keys


def item_when_ok(t, item, ctx):
    """loop 任务的 per-item when（only_kvpool 标志）。"""
    w = t.get("when")
    if w is None:
        return True
    ictx = dict(ctx, item=item)
    return ev(w, ictx) is True


def simulate_start(mode_cfg):
    base = build_base(mode_cfg)
    play = yaml.safe_load(open(os.path.join(BASE, "playbooks/start.yml")))[0]
    master_nodes = val(play["vars"]["mooncake_master_nodes"], base)

    facts = {h: dict(base) for h in groups["all"]}
    for h in groups["all"]:
        facts[h]["inventory_hostname"] = h
        facts[h].update(hostvars.get(h, {}))   # 注入该节点自身主机变量（ip/nic/idx/ansible_host）
        facts[h]["mooncake_master_nodes"] = master_nodes
    shared = {}  # run_once+delegate localhost 的 set_fact → 注入所有 host
    results = []  # (kind, host, task, cmd)

    for t in play["tasks"]:
        name = t.get("name", "?")
        when = t.get("when")

        # 决定在哪些 host 上跑
        run_hosts = []
        for h in groups["all"]:
            ctx = dict(facts[h], **shared)
            if when is None or ev(when, ctx) is True:
                run_hosts.append(h)
        if t.get("run_once"):
            if t.get("delegate_to") == "localhost":
                run_hosts = ["localhost"] if run_hosts else []
            elif t.get("delegate_to"):
                run_hosts = [t["delegate_to"]] if run_hosts else []
            else:
                run_hosts = run_hosts[:1]

        for h in run_hosts:
            # localhost 借用第一台真实 host 的 fact 基底
            src_host = h if h in facts else groups["all"][0]
            ctx = dict(facts[src_host], **shared)
            if h == "localhost":
                ctx["inventory_hostname"] = "localhost"
            tv = {k: (val(v, ctx) if isinstance(v, str) else v) for k, v in t.get("vars", {}).items()}
            ctx.update(tv)
            rname = rt(name, ctx)   # 渲染任务名（含 {{ }}）

            if "set_fact" in t:
                for k, v in t["set_fact"].items():
                    vval = val(v, ctx) if isinstance(v, str) else v
                    if t.get("delegate_to") == "localhost" and t.get("run_once"):
                        shared[k] = vval
                        ctx[k] = vval
                    elif t.get("delegate_to"):
                        facts.setdefault(h, dict(facts[src_host]))[k] = vval
                    else:
                        facts[h][k] = vval
                continue

            if "shell" in t and "loop" in t:
                for item in loop_items(t, ctx):
                    ictx = dict(ctx, item=item)
                    if not item_when_ok(t, item, ictx):
                        continue
                    cmd = rt(t["shell"], ictx)
                    results.append(("shell", h, rname + f" [{item}]", cmd))
                continue
            if "shell" in t:
                cmd = rt(t["shell"], ctx)
                results.append(("shell", h, rname, cmd))
            elif "copy" in t and "loop" in t:
                for item in loop_items(t, ctx):
                    ictx = dict(ctx, item=item)
                    if not item_when_ok(t, item, ictx):
                        continue
                    if isinstance(item, dict):
                        src = rt(item.get("src", ""), ictx)
                        dest = rt(item.get("dest", ""), ictx)
                    else:
                        src = rt(item, ictx)
                        dest = "/tmp/" + os.path.basename(str(item))
                    results.append(("copy", h, rname + f" [{item}]",
                                    f"copy {src} → {dest}"))
            elif "copy" in t:
                results.append(("copy", h, rname, rt(t["copy"].get("content", ""), ctx)))
            elif "stat" in t and "register" in t:
                shared[t["register"]] = {"stat": {"exists": True}}  # 假设已先跑 gen 生成
                results.append(("note", h, rname, "stat（假设 generated 已由 gen 生成，exists=true）"))
            elif "assert" in t:
                ok = all(ev(c, ctx) is True for c in t["assert"]["that"])
                results.append(("note", h, rname,
                                f"assert {t['assert']['that']} → {'通过 ✅' if ok else '失败 ❌'}"
                                + (f" —— {t['assert'].get('fail_msg', '')}" if not ok else "")))
            elif "fail" in t:
                results.append(("note", h, rname, "fail（when 成立才会触发；正常流程应跳过）"))
            elif "template" in t:
                tsrc = rt(t["template"]["src"], ctx).replace("../templates/", "")
                out = env.get_template(tsrc).render(**ctx)
                markers = "\n".join("      | " + s for s in tpl_markers(out))
                results.append(("template", h, rname, f"[{tsrc}] {len(out)} 字符\n{markers}"))
            else:
                results.append(("note", h, rname, "(非命令任务：file/uri 等，跳过渲染)"))
    return facts, results


def simulate_gen(mode_cfg):
    base = build_base(mode_cfg)
    play = yaml.safe_load(open(os.path.join(BASE, "playbooks/gen.yml")))[0]
    gv_vars = play.get("vars", {})
    ctx = dict(base)
    for k, v in gv_vars.items():
        ctx[k] = val(v, ctx) if isinstance(v, str) else v
    results = []
    for t in play["tasks"]:
        name = t.get("name", "?")
        if t.get("when") and ev(t["when"], ctx) is not True:
            results.append(("skip", "-", name, "(不执行)"))
            continue
        if "template" in t:
            for item in loop_items(t, ctx):
                tctx = dict(ctx, item=item)
                for k, v in t["vars"].items():
                    tctx[k] = val(v, tctx) if isinstance(v, str) else v
                tsrc = rt(t["template"]["src"], tctx).replace("../templates/", "")
                out = env.get_template(tsrc).render(**tctx)
                markers = "\n".join("      | " + s for s in tpl_markers(out))
                results.append(("template", "-", f"{name} [{item}]",
                                f"[{tsrc}] {len(out)} 字符\n{markers}"))
        elif "copy" in t:
            results.append(("copy", "-", name, rt(t["copy"]["content"], ctx)))
        else:
            results.append(("note", "-", name, "(非命令任务，跳过渲染)"))
    return results


def report(mode_key, mode_cfg, mode_label):
    from collections import OrderedDict
    print("\n" + "=" * 90)
    print(f"模式: {mode_key} — {mode_label}")
    print(f"   enabled={mode_cfg['enabled']}  enable_ha={mode_cfg['enable_ha']}  haproxy.mode={mode_cfg['haproxy_mode']}")
    print("=" * 90)

    # ---------- gen.yml ----------
    gen = simulate_gen(mode_cfg)
    print("\n## gen.yml")
    gg = OrderedDict()
    for kind, host, name, cmd in gen:
        gg.setdefault(name, []).append((kind, cmd))
    for name, entries in gg.items():
        kind = entries[0][0]
        if kind == "skip":
            print(f"- [跳过] {name}")
            continue
        if kind == "template":
            print(f"- {name}")
            for k, cmd in entries:
                first = cmd.splitlines()[0]
                print(f"    {first}")
        elif kind == "copy":
            print(f"- {name}")
            for k, cmd in entries:
                for ln in cmd.splitlines():
                    print(f"    | {ln}")
        else:
            print(f"- {name}：{entries[0][1]}")

    # ---------- start.yml ----------
    facts, start = simulate_start(mode_cfg)
    print("\n## start.yml")
    sg = OrderedDict()
    for kind, host, name, cmd in start:
        sg.setdefault(name, []).append((kind, host, cmd))
    shell_fail = []
    for idx, (name, entries) in enumerate(sg.items(), 1):
        kind = entries[0][0]
        # 按 cmd 去重，列出 host
        by_cmd = OrderedDict()
        for k, h, c in entries:
            by_cmd.setdefault(c, []).append(h)
        print(f"\n### 任务 {idx}. {name}")
        for cmd, hosts in by_cmd.items():
            if kind == "shell":
                ok = bash_ok(cmd)
                if not ok:
                    shell_fail.append((hosts, name))
                tag = "bash-OK" if ok else "bash-FAIL"
                print(f"  执行于 [{', '.join(hosts)}]  {tag}")
                for ln in cmd.splitlines():
                    print(f"    | {ln}")
            elif kind == "template":
                print(f"  ({cmd.splitlines()[0]})")
                for ln in cmd.splitlines()[1:]:
                    print(f"    {ln}")
            elif kind == "copy":
                print(f"  执行于 [{', '.join(hosts)}]")
                for ln in cmd.splitlines():
                    print(f"    | {ln}")
            else:  # note
                print(f"  [{', '.join(hosts)}] {cmd}")
    total_shell = sum(1 for k, *_ in start if k == "shell")
    print(f"\n—— 汇总：shell 命令 {total_shell} 条，语法失败 {len(shell_fail)} 条"
          + (f"：{shell_fail}" if shell_fail else " ✅"))


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    for mk, cfg, label in MODES:
        if key and key.lower() not in mk:
            continue
        report(mk, cfg, label)


if __name__ == "__main__":
    main()
