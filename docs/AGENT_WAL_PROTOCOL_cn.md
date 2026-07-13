# Agent WAL Protocol agent.wal.v1

[English version](AGENT_WAL_PROTOCOL.md)

> 状态：草案
> 适用范围：OpenEvent channel `protocol="agent.wal.v1"` 的 Agent write-ahead log payload 与 channel description

## 1. Channel 约定

每个 Agent 会话 MUST 独占一个 WAL channel。WAL channel 只保存该会话的 Agent 本地推进意图，不承载 IM 内容、模型请求正文或模型结果。

所有 Agent WAL channel MUST 设置：

```text
protocol = "agent.wal.v1"
```

`description` MUST 是 JSON 字符串：

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

字段约束：

- `version`：当前固定为 `v1`
- `session_id`：Agent 配置中的会话 ID
- `im_channel_id`：该 Agent 会话绑定的 `im.v1` channel
- `model_channel_id`：该 Agent 会话绑定的 `llm.v1` channel
- `updated_at_ms`：毫秒时间戳
- `metadata`：可选 object，用于承载部署或业务域需要的静态扩展信息

约束：

- 一个 Agent 会话只能配置一个 WAL channel。
- 一个 WAL channel 只能归属一个 Agent 会话。
- 同一 Agent 进程内 `wal_channel_id` MUST 唯一。
- WAL channel 推荐使用 `private`。
- Agent principal MUST 具备 WAL channel 读写权限。
- IM Sync Worker 与 model-proxy worker 不消费 WAL channel，也不需要 WAL channel 权限。

## 2. Payload Envelope

`agent.wal.v1` payload 是 UTF-8 JSON object。首版只定义一种日志：

```json
{
  "kind": "llm.request.prepare",
  "ts_ms": 1710000000000,
  "pre_llm_seq": 12340,
  "user_message_seqs": [12345, 12346]
}
```

公共字段：

- `kind`：必填，当前固定为 `llm.request.prepare`
- `ts_ms`：必填，Unix 毫秒时间戳（UTC）
- `pre_llm_seq`：必填，准备本次 LLM 请求前，本会话上一条 `llm.v1 infer.request` 的 OpenEvent `seq`；若本会话此前没有 LLM 请求，值为 `0`
- `user_message_seqs`：必填，本次准备提交给 LLM 的用户消息 `im.v1 sync.record.seq` 列表

协议字段严格校验：未知字段、缺失必填字段、字段类型不匹配均视为非法 payload。

## 3. OpenEvent principal 与 recipients

`principal` 是 OpenEvent EventMessage 的顶层字段，不放入 `agent.wal.v1` payload。

规则：

- `llm.request.prepare` 的 OpenEvent `principal` MUST 使用 Agent principal。
- `llm.request.prepare` 的 OpenEvent `recipients` MUST 为空数组。
- WAL channel 可见性和成员负责访问控制，不使用 `recipients` 表达接收方。
- payload 中不得包含 `source_principal`、token、API key 或模型请求正文。

## 4. llm.request.prepare

`llm.request.prepare` 表示 Agent 准备向 `llm.v1` channel 写入一条新的 `infer.request`。它必须先于对应 `infer.request` 写入 OpenEvent。

```json
{
  "kind": "llm.request.prepare",
  "ts_ms": 1710000000000,
  "pre_llm_seq": 12340,
  "user_message_seqs": [12345, 12346]
}
```

规则：

- `pre_llm_seq` MUST 等于本会话在写入 WAL 前已经确认的上一条 `llm.v1 infer.request.seq`。
- 如果本会话还没有任何 `infer.request`，`pre_llm_seq` MUST 为 `0`。
- `user_message_seqs` MUST 是非空数组。
- `user_message_seqs` 的每个元素 MUST 指向本会话 IM channel 中一条需要触发模型调用的用户 `sync.record.seq`。
- `user_message_seqs` MUST 按 OpenEvent `seq` 严格升序排列，且不得重复。
- 如果一次模型请求合并多条用户消息，`user_message_seqs` MUST 精确列出本批次所有用户消息 seq，而不是只记录最后一条高水位。
- `turn_id` 不是 WAL payload 字段；Agent 恢复时通过 WAL channel description 中的 `session_id` 和 payload 中的 `user_message_seqs[0]` 派生 `turn_id = "{session_id}:{user_message_seqs[0]}"`。
- 写入 WAL 后，对应的 `llm.v1 infer.request.request_id` MUST 使用该 WAL 消息的 OpenEvent `seq` 生成，格式为 `agent:{session_id}:wal:{wal_seq}`。
- 每一次新的 `infer.request` attempt 前都 MUST 写入一条新的 WAL 记录。任何模型 attempt
  失败导致的重试都需要新的 WAL 记录。失败 attempt 包括无匹配结果的超时、非 2xx
  `infer.result`、模型输出无法解析和非法 tool call。

## 5. 关联链

首版 Agent 使用 WAL 作为 IM 输入与 LLM 请求之间的前置提交点，但不使用 `infer.request.prev_seq` 或 `send.request.prev_seq`。跨 channel 关联通过 request_id 约定完成：

```text
im sync.record.seq in agent wal user_message_seqs
agent wal.seq => model_request_id = "agent:{session_id}:wal:{wal_seq}"
llm infer.request.request_id == model_request_id
llm infer.result.request_id == model_request_id
llm infer.result.prev_seq -> llm infer.request.seq
im send.request.request_id == "im:{model_request_id}"
im send.result.request_id == im send.request.request_id
im send.result.prev_seq -> im send.request.seq
```

如果该 turn 在拿到可接受模型结果前达到模型 attempt 上限，Agent 写冻结 `send.request`，
而不是普通模型回复：

```text
im send.request.request_id == "freeze:{turn_id}"
```

含义：

- WAL 记录本次准备提交给 LLM 的用户消息 seq 列表，以及写 WAL 时上一条 LLM 请求的 seq。
- `infer.request` 不填写 `prev_seq`；通过 `request_id` 中的 `wal_seq` 找回对应 WAL。
- `send.request` 不填写 `prev_seq`；通过 `request_id="im:{model_request_id}"` 关联到被采用的模型请求。
- 冻结 `send.request` 也是同一 `turn_id` 的终态回复，但它是 Agent 主动写出的暂停提示，
  不是模型结果回复。它使用 `request_id="freeze:{turn_id}"`，不填写 `prev_seq`。
- `infer.result.prev_seq` 和 `send.result.prev_seq` 仍按各自协议必填，只表达 result 指向同协议 request 的关系。
- 命令调用不写入 WAL payload，而是通过
  `cmd.run.request.request_id="cmd:{model_request_id}:{tool_call_index}"`
  关联。命令结果会映射为后续模型输入中的 `exec_result` event；输出读取结果会映射为
  `read_stdout_result` 或 `read_stderr_result` event；WAL 仍只保存
  `pre_llm_seq` 和 `user_message_seqs`。

## 6. 恢复语义

Agent 重启后扫描 WAL channel 时：

- 如果存在 `llm.request.prepare`，但找不到 `request_id == "agent:{session_id}:wal:{wal_seq}"` 的 `infer.request`，且该 turn 尚未被后续结果或 IM 回复关闭，Agent MUST 基于 OpenEvent 全部历史重建 prompt 和用户消息批次，并使用该 WAL 记录补发对应的 `infer.request`；无法重建时 MUST 进入 error 状态并退出，等待人工检查。
- 如果存在 `llm.request.prepare`，且已经存在 `request_id == "agent:{session_id}:wal:{wal_seq}"` 的 `infer.request`，Agent MUST 继续按该模型请求的状态恢复，不再为同一 attempt 创建新的 WAL。
- 如果该模型请求已有失败 attempt 结果，恢复时 MUST 按运行时相同的重试/冻结规则处理；
  不写普通 IM 失败回复，也不使用该失败 attempt 推进 prompt 状态。
- 如果某个 WAL 记录的 `user_message_seqs` 已经被后续完成的 turn 覆盖，且没有孤立副作用需要补偿，该 WAL 记录可视为历史记录，不触发补发。
- 如果 `pre_llm_seq` 与扫描历史恢复出的上一条 LLM 请求 seq 不一致，Agent MUST 记录一致性错误并进入 error 状态退出，避免并发 Agent 进程或配置错误造成重复推进。

WAL 只表达“准备提交”的事实，不表达模型请求已经成功发布。模型请求是否已经发布，以是否存在 `llm.v1 infer.request.request_id == "agent:{session_id}:wal:{wal_seq}"` 为准。

孤立 WAL 的补发由 Agent 设计决定，但必须遵守以下边界：

- 如果 `pre_llm_seq` 对应的上一条模型请求与当前 WAL 拥有相同 `user_message_seqs`，可视为同一 turn 的下一次 attempt。
- 如果 `pre_llm_seq` 对应的上一条模型请求拥有不同的 `user_message_seqs`，当前 WAL 表示新 turn。
- 如果 `pre_llm_seq` 为 `0`，当前 WAL 表示该 session 首轮请求。
- 如果恢复时无法基于 OpenEvent 全部历史重建 prompt 或用户消息批次，不得猜测请求正文；Agent 必须进入 error 状态并退出，等待人工处理。
- 如果某个 turn 的 attempt 数已经达到 `max_model_attempts`，且最后一个未被接受的 attempt
  已失败，Agent 将该 session 恢复为 frozen。后续用户 `sync.record` 会解除冻结并开启新 turn。

## 7. 版本策略

- `agent.wal.v1` 是严格 schema。未知 payload 字段均非法，MUST 被拒绝。
- 首版仅定义 `llm.request.prepare`；其它 `kind` 在 `agent.wal.v1` 中均非法。
- 新增 payload 字段、新增 `kind`，或改变已定义字段语义，都需要使用新的 channel protocol，如 `agent.wal.v2`。
