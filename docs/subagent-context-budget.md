# Subtask 上下文预算：把「执行」下沉，主上下文只留「判据」

## 问题

上一轮会话耗尽上下文，不是任务太难，是**执行噪音进了主上下文**。
一次「改配置 → 重启 → 复现 → 抓日志 → 判断」闭环，主上下文约吃掉 15k–25k tokens：

| 来源 | 量级 | 用途 |
|---|---|---|
| ansible-playbook 滚动输出 | 2k–5k | 只判断 ok/failed 计数 |
| 引擎健康轮询（P/D 各 2 节点） | 1k–3k | 只判断 200 / 000 |
| 4 节点 vllm 日志 tail | 4k–10k | 只找 ERROR 行 |
| 客户端异常栈 | 1k–2k | 只取异常类型 |
| 设备 slog 摘录 | 2k–5k | 只取 1–3 行根因 |

这些内容**读一次、判一次、之后永不回看**，却全程占着窗口。

## 分界线

- **下沉**：输出量大、只服务一次性判断的工作（部署、轮询、抓日志、跑 bench、比对输出）。
- **留下**：跨轮推理需要的状态（当前配置、当前假设、已排除项、上次实验结论）。

判断标准只有一句：**这个东西会被下一轮引用吗？** 不会 → 下沉。

## 四条规则

### 1. 回报契约：固定字段，≤10 行，禁止贴日志

prompt 里写死回报形状，例如：

```
只回报这一行，不要别的：
{"engines":"4/4-200","proxy":"ok","took_s":742}
```

失败时也只回报分类，不回报栈：

```
{"engines":"p0-000","proxy":"err","took_s":742,"first_err":"TBE Subprocess"}
```

### 2. 中间产物落盘，回报路径

大块证据写文件，回报 `path` + 3 行摘要：

```
把 playbook 完整输出写 /tmp/reuse-diag/deploy-<ts>.log，
只回报最后 15 行里的 failed= 数字和失败 task 名。
```

需要细节时主侧再定向 `grep`，一次只取命中行。

### 3. prompt 自包含：给命令 + 给判据，不给背景故事

subagent 看不到主上下文，背景写得再多它也只能自己重新探索，反而更贵。有效写法是：

- 准确命令（可直接粘贴执行）
- 期望输出形状
- 成功/失败判据（`health==200` 算成功，`000` 算失败，超时 120s 算失败）
- 禁止事项（不要重启、不要改配置、不要全量 cat 日志）

### 4. 不用 fork；fan-out 用 workflow + schema

`subagent_fork` 会把整段对话历史灌给子代理，上下文成本翻倍，只在「必须基于本轮推理做判断」时用（如代码 review）。
纯执行类一律 `subagent`。
多个独立探测（4 节点并行取日志 / 多档 prompt 长度）用 `workflow` + `opts.schema`，回报天然是结构化小对象。

## 收益估算

下沉后主上下文每轮 ~300 tokens（一个 JSON）。按每轮省 20k 计，**10 轮 ≈ 200k，正好是一个完整上下文窗口** —— 也就是上一轮死掉的那个量。

## 本项目的固定套路

```bash
# 目录约定
/tmp/reuse-diag/deploy-<ts>.log     # playbook 全量
/tmp/reuse-diag/repro-<len>.json    # 单次冷请求结果
/tmp/reuse-diag/slog-<node>.txt     # 设备日志命中行
```

三个阶段各自一个 subagent：
1. **deploy**：返回 `{engines, proxy, took_s, failed_tasks[]}`
2. **repro**：返回 `{pt, ok, err_class, took_s}`
3. **slog**：返回 `{node, hits:n, first_hit}` —— 命中行写文件，不回报正文
