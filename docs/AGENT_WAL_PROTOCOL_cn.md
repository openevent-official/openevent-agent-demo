# Agent WAL Protocol agent.wal.v1

[English version](AGENT_WAL_PROTOCOL.md)

> 状态：草案
> 适用范围：OpenEvent channel `protocol="agent.wal.v1"` 的 payload 与 channel description

## 1. 文档职责

本文是 `agent.wal.v1` 字段、稳定 ID、发布对账、retry/blocked 和恢复语义的唯一协议事实来源。
Agent 的持续运行概览见 [IM_MODEL_AGENT_cn.md](IM_MODEL_AGENT_cn.md)。

## 2. Channel

每个 Agent session 独占一个 WAL channel，设置 `protocol="agent.wal.v1"`。WAL 记录下一条模型请求
将消费哪些新事实，不保存模型请求正文或命令输出正文。

`description`：

```json
{
  "version": "v1",
  "session_id": "agent-session-001",
  "im_channel_id": 10001,
  "model_channel_id": 20001,
  "updated_at_ms": 1710000000000,
  "metadata": {}
}
```

WAL channel 推荐为 `private`。Agent principal 必须具备读写权限；其它 worker 不消费 WAL。

本协议假定同一 `session_id` 初始化后永久绑定同一组 IM、Model、WAL、Cmd Channel；所有 session 内的
消息引用都在这组固定绑定下解释。更换任一 Channel 必须创建新的 `session_id`。绑定生命周期由
[Runtime Reconciler](RUNTIME_RECONCILER_cn.md#channel-协调) 定义。

## 3. Payload

协议定义 `llm.request.prepare` 和 `cmd.request.timeout`。Prepare payload：

```json
{
  "kind": "llm.request.prepare",
  "prepare_id": "prepare:agent-session-001:9d70c7",
  "ts_ms": 1710000000000,
  "pre_llm_seq": 12340,
  "user_message_seqs": [],
  "input_event_refs": [
    {
      "type": "exec_result",
      "seq": 12350
    }
  ]
}
```

字段：

- `kind`：必填，固定为 `llm.request.prepare`。
- `prepare_id`：必填、非空；同一 WAL channel 内唯一，标识这一份确定的新增模型输入。
- `ts_ms`：必填 Unix 毫秒时间戳。
- `pre_llm_seq`：必填；必须等于写 WAL 前该 session 最新的 `infer.request.seq`，首次为 `0`。
- `user_message_seqs`：必填数组，引用新增用户 `sync.record.seq`，没有时为空。
- `input_event_refs`：必填数组，引用新增工具结果，没有时为空。

`user_message_seqs` 与 `input_event_refs` 至少一个非空，也可以同时非空。两个数组均按 seq 严格升序，
不得重复。

`input_event_refs` 元素严格包含：

| 字段 | 规则 |
| --- | --- |
| `type` | `exec_result`、`read_stdout_result`、`read_stderr_result` 或 `cmd_timeout` |
| `seq` | 对应 Cmd result 或 WAL `cmd.request.timeout` 的 OpenEvent seq |

引用消息必须属于当前 session 配置的 IM/Cmd/WAL channel，并通过 `prev_seq`、`target_seq`、
`cmd_request_id` 等协议关系校验。
所有引用 seq 必须小于当前 WAL seq；已经被另一条 canonical prepare 消费的引用不得再次使用。
未知字段、未知类型、缺失字段或错误类型均为非法 payload。

## 4. Prepare ID

`prepare_id` 在第一次发布前生成，在该 WAL 发布的所有传输重试中保持不变。它只标识这一份输入引用，
不表示任务、阶段、开始或结束。不同输入集合必须使用不同 ID；完全相同的逻辑发布必须复用原 ID和
payload。

对应模型请求：

```text
model_request_id = "agent:{session_id}:wal:{wal_seq}:retry:{retry_index}"
```

一条 WAL 固定本次消费的新输入引用，可以对应多个模型 retry。`retry_index` 从 `1` 开始递增；每个
retry 都严格按该 WAL 的引用和它之前已接受的上下文构造，不得混入后来排队的事件。请求正文不要求
逐字节相同，retry 也不新增 WAL。

## 5. 发布对账

发布 WAL 时：

1. 发布前用 `GetStatus` 记录 `max_seq`。
2. 调用一次 `PublishAutoSeq`。
3. 成功时使用返回 seq。
4. 结果不确定时获取新的确定水位，用 `Fetch(channels=[wal_channel_id])` 完整扫描两个水位之间。
5. 找到相同 `prepare_id` 和内容时复用最小 seq；相同 ID内容不同属于一致性错误。
6. 完整扫描确认不存在后，才允许用相同 ID和内容重发。

`infer.request` 和 IM `send.request` 采用相同的稳定 ID模式。`cmd.v1` request payload 没有稳定 ID；
Agent 必须在发布前固定完整 payload，结果不确定时扫描发布前后水位区间，只接受由 Agent principal
发布且 payload 完全一致的 request。完整扫描确认不存在后才允许重发同一 payload。只查看 channel
最后一条消息不能完成对账。

## 6. 模型文字和工具

模型结果的每段非空 `content` 使用独立 IM 去重键：

```text
send.request.request_id = "model-content:{model_request_id}"
```

无论同一模型结果是否包含工具调用，都使用这一规则。`content` 为空时不写 IM 消息。

工具调用使用：

```text
cmd_request_id = "cmd:{model_request_id}:{tool_call_index}"
```

`cmd_request_id` 是 Agent 状态和 WAL timeout 使用的业务关联键，不写入 `cmd.v1` payload。
`cmd.v1` 任务 ID是实际 request 消息的 OpenEvent `seq`。工具结果进入下一条 WAL 的
`input_event_refs`；这些关系只关联具体消息和具体调用，不表达更高层任务状态。

Cmd request 超时事件写入当前 session 的 WAL channel：

```json
{
  "kind": "cmd.request.timeout",
  "timeout_id": "cmd-timeout:cmd:agent:agent-session-001:wal:12345:retry:1:0",
  "ts_ms": 1710000300000,
  "cmd_request_id": "cmd:agent:agent-session-001:wal:12345:retry:1:0",
  "cmd_request_seq": 12350,
  "tool_call_id": "call_exec_1",
  "tool_name": "exec"
}
```

`timeout_id = "cmd-timeout:{cmd_request_id}"`，在 WAL channel 内唯一。`cmd_request_seq` 必须指向当前
session Cmd channel 中内容匹配的请求；`tool_call_id` 和 `tool_name` 必须匹配已接受的 assistant
tool call。达到 `agent.cmd_result_timeout_ms` 后，Agent 必须先记录 Cmd channel 确定水位并 Fetch 到该
水位；只有确认结果不存在，才按第 5 节的发布对账算法写超时事件。相同 ID 内容冲突属于一致性错误。

超时事件映射为对应 `role=tool` 的 `status="error"`、`error_code="timeout"` 结果，`error_message`
明确表示等待 Cmd 结果超时。它一旦发布，就成为该 tool call 的唯一已接受结果；后来到达的真实 Cmd
result 标记为 late，不进入 prompt。若对账时先发现真实结果，则不发布超时事件。

## 7. Retry

- 当前 retry 明确失败或超过 Agent 等待超时后，才允许发布下一个 retry。
- 新 retry 使用新的 request ID；model-proxy 因而将其视为新的模型调用。
- 超时只表示 Agent 不再等待旧请求，不要求 model-proxy 或 Provider 已经停止处理。新 retry 发布后，
  多个模型调用可以同时在 Provider 侧执行。
- 并发模型调用只允许带来 token、限流和容量开销，不能改变 Agent 状态语义。模型调用本身必须是无
  业务副作用的推理；只有当前最高 `retry_index` 的有效结果可以产生 IM、Cmd 或 prompt 副作用。
- 新 retry 一旦发布，更小 `retry_index` 的迟到结果全部为 stale，不发送 content、不执行工具、不推进 prompt。
- 发布 retry 的结果不确定时，必须先按该 retry 的 request ID完成历史对账，不能直接增加 index。
- 达到 `max_model_attempts` 后，WAL 保持 blocked；它引用的输入和后续排队事件都不得被跳过。
- 明确解除 blocked 后，从历史最大 retry index 的下一个值继续。

## 8. 恢复

- 相同 `prepare_id` 和相同 payload 出现多条：采用最小 seq，其余为重复日志。
- 相同 `prepare_id` 内容不同：一致性错误。
- `pre_llm_seq`、输入所属 session、引用类型、引用因果关系或引用唯一性不匹配：一致性错误。
- WAL 存在而模型请求不存在：按精确输入引用重建并补写请求。
- 当前模型请求存在而结果不存在：未超时继续等待；超时后按上限决定发布下一 retry 或保持 blocked。
- 旧 retry 的迟到结果：标记 stale，不产生副作用。
- 当前 retry 的结果已完整校验并接受，且有非空 `content`，但对应
  `model-content:{model_request_id}` 不存在：对账后补写 IM request。
- 当前 retry 的结果已完整校验并接受，且有工具调用，但对应 Cmd request 不存在：按工具顺序和确定
  payload 补写；恢复时也按顺序和 payload 将现有 request 关联到 `cmd_request_id`。
- 工具结果没有被后续 WAL 引用：重新加入 session 输入队列。
- Cmd request 没有结果且未超时：继续等待；已超时则先对账，仍无结果时补写 `cmd.request.timeout`。
- `cmd.request.timeout` 没有被后续 prepare 引用：以 `cmd_timeout` 重新加入 session 输入队列。

恢复不判断一次工作是否开始、完成或关闭。

## 9. 版本

- `agent.wal.v1` 使用严格 schema。
- 当前仍是草案，以本文字段为准，不兼容此前草案数据。
- 稳定发布后的破坏性变化必须使用新协议版本。
