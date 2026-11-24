# Agent WAL 协议 agent.wal.v5

> 实现状态：当前实现使用 `agent.wal.v5`。

[English version](AGENT_WAL_PROTOCOL.md)

## 1. 范围和 Channel

本文是 Chat Agent 持久记录、字段及源引用的唯一协议说明。配置和消息额度见
[Chat Agent](CHAT_MODEL_AGENT_cn.md)，部署资源见 [Runtime](RUNTIME_RECONCILER_cn.md)。
本协议定义 `agent.wal.v5`：普通回答固定一份事件输入，工具独立执行，压缩固定源范围并保存摘要引用。
WAL 保存决定和来源，不复制完整模型请求、回答、工具输出或摘要正文。

一个部署的全部 Chat 会话共享一个 private WAL Channel；唯一成员和业务记录 writer 是 Agent principal。
`protocol` 固定为 `agent.wal.v5`。Runtime 创建的 description：

```json
{"version":"v5","metadata":{"deployment_id":"demo"}}
```

部署永久绑定同一组身份及 Model、Cmd、WAL Channel。会话记录用 Chat Channel ID 区分。
恢复依赖仍被引用的 WAL、Chat、Model、Cmd 原始事件和附件对象，不能只保留本地缓存。
OpenEvent 写入的 Channel 系统消息不是 WAL 业务记录；公共读取入口先按系统协议分流，
具体见 [Agent 会话发现](CHAT_MODEL_AGENT_cn.md#会话发现)。

## 2. 记录外层和稳定键

payload 是 UTF-8 JSON，外层恰好包含以下字段。空 data 仅展示外层，实际字段见下节。

```json
{"version":5,"kind":"answer.prepare","key":"chat:30:answer:0","chat_channel_id":30,"data":{}}
```

| 字段 | 类型与含义 |
| --- | --- |
| `version` | integer，固定为 `5` |
| `kind` | 下节规定的记录种类 |
| `key` | 非空 string，在整个 WAL Channel 内标识一个不可变事实 |
| `chat_channel_id` | 非负 integer；部署记录为 `0`，会话记录为实际 Channel ID |
| `data` | object，配置、裁决或源事件引用 |

记录时间和位置取 OpenEvent 顶层 `ts_ms` 和 `seq`。模型输入中事件的排列位置按第 4 节从来源确定，
不能直接用保存它的 WAL 记录位置代替。同键、同种类、同会话、同数据的重复记录取最小 seq；同键内容不同是冲突。
未知版本、未知种类、非法外层或其他 principal 写入 WAL 业务记录均停止进程。

以下缩写只用于表格，不是额外字段：

| 缩写 | 完整键或前缀 |
| --- | --- |
| `A` | 回答键 `chat:<channel_id>:answer:<回答序号>` |
| `C` | 压缩批次键 `chat:<channel_id>:compaction:<批次序号>` |
| `P` | 尝试键 `<A或C>:attempt:<尝试序号>` |
| `T` | 普通工具前缀 `<P>:tool:<调用序号>` |
| `R` | 拒绝键 `chat:<channel_id>:input:<原用户seq>:reject` |
| `E` | 下方动作表定义的副作用动作键 |

数字均用十进制表示。回答与压缩批次序号分别从 0 开始，按本会话各自已确认的准备记录数量递增；
尝试序号在所属回答或批次内从 0 开始，调用序号在所属普通尝试内按模型调用顺序从 0 开始。
每种记录的完整键与归属如下，不能仅按 `kind` 猜测是否追加 `:prepare`：

| kind | 完整记录键 | 归属或引用约束 |
| --- | --- | --- |
| `deployment` | `deployment:<deployment_id>` | `chat_channel_id=0`；ID 与 `data.deployment_id` 一致 |
| `input.reject` | `R` | 键中的源 seq 等于 `data.input_seq` |
| `answer.prepare` | `A` | 所属 Chat 会话 |
| `compaction.prepare` | `C` | 所属 Chat 会话 |
| `attempt.prepare` | `P` | `data.owner` 为键中的 `A` 或 `C`；`purpose` 与所属准备种类一致，`number` 等于尝试序号 |
| `attempt.accept` | `<P>:accept` | `data.attempt=P` |
| `attempt.timeout` | `<P>:timeout` | `data.attempt=P`；该尝试的唯一到期触发事件 |
| `attempt.fail` | `<P>:fail` | `data.attempt=P` |
| `tool.prepare` | `<T>:prepare` | `data.tool_key=T`；`data.answer` 为该普通尝试所属的 `A` |
| `tool.timeout` | `<T>:timeout` | `data.tool_key=T`；该工具的唯一到期触发事件 |
| `tool.done` | `<T>:done` | `data.tool_key=T`，引用 `<T>:prepare` |
| `answer.cancel` | `<A>:cancel` | 去掉末尾 `:cancel` 得到所属 `answer.prepare` 键 |
| `answer.freeze` | `<A>:freeze` | 所属 `A`，其失败通知见 `notice_seq` |
| `answer.wake` | `<A>:wake` | 解除 `<A>:freeze`，用户来源见 `input_seq` |
| `compaction.commit` | `<C>:commit` | `data.attempt` 必须属于 `C`，并有对应的 `attempt.accept` |
| `effect.prepare` | `<E>:prepare` | 副作用动作 `E` 的固定发送意图 |
| `effect.commit` | `<E>:commit` | 对应 `<E>:prepare`，引用实际提交的业务事件 |
| `effect.error` | `<E>:error:<随机UUID字符串>` | `data.effect_key=E`；同一动作可保存多份发布证据，随机后缀区分证据记录，不是消息 UUID |

除部署记录外，键中的会话、外层 `chat_channel_id` 与所引用的所属记录必须一致。`T` 和 `E` 是前缀，
本身不对应一条独立记录；其他归属按表中引用回到准备记录。副作用动作键只在此定义：

| 动作键 `E` | 模块与动作 | 归属和用途 |
| --- | --- | --- |
| `<A>:start` | Chat `turn.start` | 普通回答的占位回复 |
| `<P>:placeholder` | Chat `turn.reset` | 普通重试前恢复占位 |
| `<P>:text:<start_seq>` | Chat `turn.reset` 或 `turn.append` | 普通展示批次；`start_seq` 等于其模型 `source.start_seq` |
| `<P>:final` | Chat `turn.reset` | 最终正文或拒答整体替换；完整 tool-only 响应覆盖临时正文时恢复占位 |
| `<A>:failure-cancel` | Chat `turn.cancel` | 普通回答失败收尾 |
| `<A>:notice` | Chat `turn.single` | 独立失败通知，通知 `turn_id` 为该回答的 `turn_id` 加 `:failure` |
| `<A>:tool-completion` | Chat `turn.reset` | 仅工具回复的结束提示 |
| `<A>:end` | Chat `turn.end` | 普通回答结束 |
| `<R>:notice` | Chat `turn.single` | 拒绝通知，使用该 `input.reject.turn_id` |
| `<P>:request` | Model `infer.request` | 该普通或压缩尝试的模型请求 |
| `<P>:cancel` | Model `infer.cancel` | 取消同一尝试的已提交请求 |
| `<T>:request:<页序号>` | Cmd `cmd.run.request` 或 `cmd.output.read.request` | exec 只使用第 0 页；read 从 0 起连续编号，每页各有意图与确认 |
| `<T>:kill` | Cmd `cmd.kill.request` | 请求停止该 exec 工具的实际执行，目标为其 run request seq |

动作键的 `A`、`P`、`T` 或 `R` 确定所属记录；以 `P` 为前缀的 Chat 动作只属于普通尝试。动作键及其准备、确认不能因重试或恢复改名。
回答和拒绝分别保存随机 `turn_id`，尝试保存共享 Model Channel 内唯一的随机 `stream_id`。
工具准备记录保存随机 UUID 字符串 `task_id`；它是模型可见任务标识，不是 OpenEvent 消息 UUID，也不是 Cmd `exec_id`。
拒绝不占用回答或模型尝试序号。

## 3. data 字段

seq 均为 OpenEvent 全局事件序号，记录键均引用本 WAL。配置和参数不包含凭据。

| kind | data |
| --- | --- |
| `deployment` | `deployment_id`；身份整数 `agent`、`user`、`model_principal`、`cmd_principal`；Channel 整数 `model`、`cmd`、`wal` |
| `input.reject` | `input_seq`：被拒绝的用户 `turn.single` 源 seq；`reason`：固定为 `message_too_long`；`snapshot`：判定所用的读取与处理水位；`turn_id`：拒绝通知的稳定随机 ID |
| `answer.prepare` | `events`：本次完整输入中未被历史摘要覆盖的有序事件引用；`input_through`：本次确认消费到的事件游标；`summary`：历史 `compaction.commit` 键或 null；`snapshot`：第 4 节的读取与处理水位；`reply_to_seqs`：冻结的用户 Chat 回复目标；`settings`：普通模型配置；`turn_id`；`max_model_attempts` |
| `attempt.prepare` | `purpose`：`answer` 或 `compaction`；`owner`：对应准备记录的键；`number`：尝试序号；`stream_id` |
| `attempt.accept` | `attempt`；`request_seq`、`stream_id`、`terminal_seq`：唯一接受的模型输出来源。普通尝试须先完整校验并确认仍适用的 Chat 展示；更晚的有效取消已关闭回复时不补展示，但保留在先结果。压缩尝试只校验摘要，不展示、不登记工具、不进入普通回答历史 |
| `attempt.timeout` | `attempt`：到期的普通或压缩尝试；本记录的 seq 是到期触发位置，不是最终失败裁决 |
| `attempt.fail` | `attempt`、正整数 `source_seq`、面向用户的固定短诊断字符串 `reason`、boolean `retryable`。`source_seq` 引用触发失败的原模型事件、`attempt.timeout` 或所属本地准备事实；`reason` 说明失败阶段及实际原因。`retryable` 的取值见[模型失败处理](CHAT_MODEL_AGENT_cn.md#会话和回答)；尚未准备的单条拒绝用 `input.reject`。裁决保存后不因迟到输出或配置变化撤销 |
| `effect.prepare` | 目标 `channel` 及第 5 节的模块字段 |
| `effect.commit` | `seq`、`uuid`：已提交的原始业务事件。SDK 已返回 seq、但尚未读到原事件时 uuid 可为 null |
| `effect.error` | `effect_key`；SDK 提供的 `state`（`COMMITTED`、`NOT_COMMITTED`、`UNKNOWN`）；可为 null 的 `uuid` |
| `tool.prepare` | 第 6 节的工具意图及稳定 `task_id` |
| `tool.timeout` | `tool_key`；`request_seq`：该工具的首个 Cmd 请求。分页读取所有页共用同一到期触发事件；本记录不是最终工具结果 |
| `tool.done` | 第 6 节的唯一工具结果裁决 |
| `answer.cancel` | `cancel_seq`：对该回答生效的用户 Chat cancel；观察到有效取消后记录，表示停止该回答及其直接工具，尚不表示清理完成 |
| `answer.freeze` | `notice_seq`：已确认的失败通知 single；暂停普通推理 |
| `answer.wake` | `input_seq`：自身内容和大小检查已通过、用于解除冻结的用户 single 源事件，必须晚于失败通知；已拒绝输入不可使用 |
| `compaction.prepare` | `snapshot`；`settings`：压缩配置；`summary`：此前采用的历史摘要提交键或 null；`events`：本批完整旧事件的有序源引用；`range`：`after`、`through` 事件游标，覆盖本批连续旧前缀 |
| `compaction.commit` | `attempt`：本批唯一接受的压缩尝试；来源、覆盖范围和配置沿该尝试回到所属 `compaction.prepare` |

普通 `settings` 含 `model`、可为 null 的 `provider`、完整 `system_prompt` 正文、`context_max_input_bytes`、
派生正整数 `context_history_max_bytes`、`io_version=4`。压缩 `settings` 使用完整 `context_compression_prompt` 正文代替 `system_prompt`，其余字段相同。
`context_max_input_bytes` 保存用户配置的原始上限 `B`，不写入当前发布容量裁剪后的有效上限。
`context_history_max_bytes` 保存本次准备实际采用的正整数目标，不是新增用户配置项。
正常情况下，普通与压缩准备保存同一套预算推导的 `T`，用于单事件准入、历史硬目标和摘要输出验收。
有效预算不足时，普通准备可保存配置预算的准入目标，新的压缩准备仍保存实际发送容量下的压缩目标；
压缩请求的 `max_summary_bytes` 从该字段取得。例外条件及目标为 0 时的失败回退统一见[Agent 上下文字节预算](CHAT_MODEL_AGENT_cn.md#上下文字节预算)，不新增预算字段。
重试与恢复沿用冻结值，不能依赖当前另一份提示词或发布容量重新推算；实际继续发布仍检查当前发送额度。
路径不是提示词快照；恢复不能重新读当前文件替换正文。完整请求 body 的 UTF-8 JSON 字节预算、压缩输出上限、
每批最多两次尝试和等待超时的具体规则统一见公开 Agent 文档，不在本协议另设参数或公式。

`attempt.fail.reason` 是失败通知的持久原因来源，使用 Agent 定义的短诊断，不复制任意原始模型字段。
`source_seq` 保留实际失败来源的位置，不用较晚的 `attempt.fail.seq` 替代；发布前本地检查失败引用所属准备记录。
本次压缩最终失败导致其回退普通回答本地失败时，该回答的失败记录承接该批次的阶段和原因，不能改写为当前用户消息过长，也不继承其他历史批次的失败。
通知所用原因及正文规范见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)；不另存一份通知原因或正文。

普通回答的输入在 `answer.prepare` 确认后固定；全部尝试使用同一事件引用、摘要、消费位置、水位和配置。
仅确认 `answer.prepare` 才推进普通输入消费游标，不因拒绝某条消息、开始压缩、读取消息或创建工具意图而推进。
`events` 只保存本次未被摘要覆盖的旧事件和新事件。前后消费游标及原来源确定新消费范围，
其中部分非触发历史可能已被摘要覆盖，不能仅凭 `events` 重建整个消费范围。
普通准备须包含至少一条尚未消费的有效用户输入或可触发推理的工具结果；已覆盖历史输出不会单独触发普通准备。
回复目标在准备时选定，恢复不重新推导。

发布前本地失败使用相同的 `answer.prepare` 保存候选来源、回复目标和配置，预算例外只见 [Agent 上下文字节预算](CHAT_MODEL_AGENT_cn.md#上下文字节预算)。
该准备确认后照常推进消费游标；对应的 `attempt.prepare` 和 `retryable=false` 的 `attempt.fail` 不要求此前已有模型请求。
本地失败已确认且尚未创建正常 Chat 回复时，不为其保存 start、reset、end 或 cancel 意图，也不创建 Model 请求意图；预留的 turn_id、stream_id 不代表已发布。
最终失败、通知的 `effect.commit` 与对应 `answer.freeze` 确认后，本次失败处理完成，不等待不存在的正常回复终态。
独立失败通知的 single 不能充当正常回复的 end/cancel；不新增完成记录或字段。

`input.reject` 用于尚未进入任何 `answer.prepare` 的单条用户输入；完成该消息自身的来源与大小检查后即可确认。
`input_seq` 必须不大于 `snapshot.read_through_seq`，连续处理水位可以落后于它。
同一源消息不能同时具有拒绝和模型消费事实；判定条件和用户行为见[过长用户消息](CHAT_MODEL_AGENT_cn.md#过长用户消息)。
确认拒绝永久排除该独立模型事件，但不推进普通消费游标，不改变其他源消息的处理状态。
原正文、附件与引用内容不复制进拒绝记录，也不保存另一份模型输入或提示词；恢复沿用既定拒绝，不按当前预算重判。
拒绝通知使用第 2 节的固定动作键及该拒绝的原 turn，完成状态由通知的 `effect.commit` 推导，
不新增拒绝完成记录、`answer.prepare`、模型尝试或冻结记录。

模型阶段完成由 `attempt.accept` 及该输出的全部 `tool.prepare` 推导，不另增完成记录或模型可见接收回执。
整条回复的收尾状态从第 6 节规定的原始事实推导；已持久确认的结果、失败和取消不在恢复时重新裁决。
模型与工具调度、冻结解除和取消行为统一见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)。

等待期限按 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)计算，不另存 deadline 或 timeout 配置副本。
到期通过 `attempt.timeout` 或 `tool.timeout` 进入同一事件序列，再与原始结果、失败及有效取消按源位置裁决；
已存在更早结果时，到期触发记录不撤销该结果。恢复复用既有触发事件，不因重新计时增加第二条。
显式 `exec.timeout_ms` 是命令参数，随工具及 Cmd 意图保存；分页读取的首请求由 `tool.done.request_seq` 引用。
未留存持久裁决的纯内存状态不作为恢复事实。

## 4. 事件引用、顺序与压缩检查点

事件引用只指定源记录，不复制模型可见正文。模型输入格式由[模型 I/O 契约](MODEL_IO_cn.md)定义。

| `type` | 引用字段 |
| --- | --- |
| `user` | `seq`：用户 `turn.single` 的源 Chat seq |
| `assistant` | `record`：普通 `attempt.accept` 键 |
| `tool_call` | `record`：`tool.prepare` 键 |
| `tool_result` | `record`：`tool.done` 键 |
| `cancel` | `record`：`answer.cancel` 键 |

事件身份由 `type` 和源引用确定；工具结果是否使用固定短错误呈现不改变身份、任务标识或排列位置。
压缩尝试的接受记录不产生普通 `assistant` 或 `tool_call` 事件。

事件游标为 `{"seq":123,"index":0}`，按 seq、index 依次比较；null 表示第一个事件之前。
`user` 用源 Chat seq；普通 `assistant` 用接受输出的 `terminal_seq`，index 为 0；同一输出的
`tool_call` 用同一 terminal seq，index 按模型调用顺序从 1 开始。
完整 `exec`、`read` 工具结果用形成该完整结果的最后必要 Cmd 源事件 seq，即 `tool.done.result_seq`，index 为 0。
`timeout` 工具结果用对应 `tool.timeout` 的 seq 和顶层 `ts_ms`，index 为 0；
用户 `cancel` 用 `answer.cancel.cancel_seq` 指向的真实 Chat 取消事件的 seq 和顶层 `ts_ms`，index 为 0。
`cancelled`、`read_cancelled` 工具结果使用同一真实 Chat 取消来源，index 为原工具调用序号加 1；
因此同一次取消先呈现用户取消，再按原调用顺序呈现直接工具的取消结果，各事件游标保持唯一。
`answer.cancel` 只是保存该取消事实，不能用它较晚的记录位置或时间替换源事件。保存、合并或压缩正文的耗时不改变这些位置。

`snapshot` 恰好包含 `read_through_seq` 和 `processed_through_seq`：前者是统一读取器已经扫描完的源区间上界，
也限定可使用的 WAL 事实；后者是在该区间内已经完成判定、整理和必要记录确认的连续处理水位，不能超过前者。
已收到完整结果但尚未保存的事件占住自己的位置；其后的事件不能越过它进入输入或历史摘要。
模型终态已收到、尚待校验或登记工具的情况遵守同一规则。工具尚无完整结果时不凭空占一个未来位置。

普通输入的范围由 `summary` 引用的摘要覆盖末游标（无摘要时为 null）与 `input_through` 推导，前开后闭，不另存 `history` 字段。
压缩的 `range` 表示 `(after, through]`。两种准备记录的 `events` 都必须按顺序精确覆盖其选定范围内的模型事件，
不能漏掉中间事件。已确认 `input.reject` 的用户源不生成模型事件；排除依据必须位于该输入或压缩的固定读取水位内。
拒绝通知也不生成 assistant 事件。后来的用户显式引用被拒绝消息时，按新用户事件的回复引用重建，不恢复原独立事件。
摘要覆盖位置可暂时领先上次普通消费位置，但普通准备必须选入摘要之后的新触发事件，
因此该次 `input_through` 严格晚于所用摘要的覆盖末游标。范围不得超出固定处理水位；确认保存某个旧源事件所用的 WAL 可以晚于其排列 seq，
但必须在固定读取水位内。由这些引用重建时必须得到同一输入，不按当前摘要或当前处理进度重新选择。

历史压缩保存“此前摘要＋接下来一段连续完整历史”的来源；`range.after` 必须等于此前摘要的覆盖末游标，没有摘要时为 null。
`range.through` 可以超过上次普通消费位置，但不能超过固定处理水位，也不能包含或跨过该快照中第一条尚未消费的
有效用户输入或可触发推理的工具结果。由此可直接覆盖已确认的历史输出与取消事实，不提前消费新任务。
普通消费位置取快照内已确认的 `answer.prepare`；摘要覆盖位置取已确认的 `compaction.commit`，分别恢复，不另存游标记录。
本批 `events` 非空，按[模型 I/O v4](MODEL_IO_cn.md#42-摘要格式与压缩调用)重建完整事件，不能切割单个事件，也不另设工具结果压缩。
同次接受输出中的多个完整事件可分属相邻压缩批次，由现有 seq、index 游标定位；完整输出的接受及工具准备规则不变。
事件来源、范围与配置在准备确认后固定。`compaction.commit` 必须引用满足冻结输出目标的已接受尝试，
覆盖至少一个完整事件并将覆盖位置推进至 `range.through`，可被普通输入或下一批压缩的 `summary` 引用。
只有准备而没有提交时，已采用的摘要及覆盖位置不变；只有本批提交也不代表整个历史已经达标。
整体是否达标由原事件、摘要及冻结目标重建，不保存字符区间、中间摘要链或额外完成标志。
最终失败的批次由既有 `attempt.fail` 判定；后续新批次即使选择相同范围，也使用新的准备键和尝试记录，旧记录保持不变。
批次选择、重试额度、失败回退和后续处理统一见 [Agent 上下文字节预算](CHAT_MODEL_AGENT_cn.md#上下文字节预算)。

工具参数、附件选择、归属、结果裁决和实际执行状态仍引用原工具、Cmd、取消及 Chat 记录，不能从摘要文字恢复。
摘要正文与覆盖边界固定，原数据和对象引用继续保留；摘要中的任务与附件信息规则见[模型 I/O](MODEL_IO_cn.md#41-摘要中的任务信息)，
WAL 不另存任务状态、完整参数附表或附件目录。

## 5. 副作用意图和显示来源

`effect.prepare.data` 除 `channel` 外，按 `module` 携带以下字段：

| module | 字段 |
| --- | --- |
| `chat` | 动作 `kind`、`turn_id`；append/reset/end 预期链尾 `pre_seq`，其他动作为 null；内容 `source` |
| `model` 请求 | `kind="infer.request"`、`attempt`、`stream_id`；从该尝试的 purpose 和 owner 重建固定输入 |
| `model` 取消 | `kind="infer.cancel"`、`stream_id`、`request_seq` |
| `cmd` | `kind`、`uuid`、`tool_key`、`request` |

普通回答的 Chat start 和 single 从所属 `answer.prepare` 取得固定回复目标；
拒绝通知 single 的回复目标固定为所属 `input.reject.input_seq`。发送意图不重复保存 `reply_to_seqs`。
append、reset、end 和 cancel 不使用回复目标。每条回复独立恢复 writer 和终态，不能用当前模型调用的回答替代旧工具的归属。

每个 Cmd 请求的 OpenEvent UUID 只在自己的 `effect.prepare` 中分配并保存；首请求、后续读取页和 kill 使用同一规则。
确认意图后才发布，重试或恢复沿用该 UUID。尚未形成发送意图时无需分配消息 UUID，也不能发布请求。

Chat `source` 可为 null（无正文动作）、`{"type":"placeholder"}`、`{"type":"failure"}`、`{"type":"tool_completion"}`，
或 `{"type":"input_rejection","record":"<input.reject键>"}`。后者仅用于所属拒绝通知 single，正文由所引用的固定 reason 按 Agent 使用契约取得。
`failure` 仅用于所属普通回答的失败通知 single；动作键确定该回答，从最终结束该回答的已确认 `attempt.fail.reason` 重建原因。
先确认最终失败裁决，再准备通知意图；恢复使用同一裁决，不根据当前模型状态或后来请求改写原因，`source` 不增加字段。
`tool_completion` 仅用于所属回答的工具结束提示 reset；动作键与归属见第 2 节。
正文及使用条件见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)；不另存完成标志、正文或模型尝试。
状态提示的文案及原因呈现由 Agent 定义。模型文本来源格式：

```json
{"type":"model","attempt":"...","field":"content","start_seq":210,"end_seq":213}
```

模型 `source` 恰好包含以上五个字段。`attempt` 必须是普通回答尝试；它及其请求提交记录定位模型流和原请求。
`start_seq`、`end_seq` 是正整数，要求 `start_seq <= end_seq`，两端都包含。范围只选取该尝试的合法模型事件，
不包含其它 Channel 或其它流的事件。

| `field` | 范围和文本含义 |
| --- | --- |
| `content` | 两端是本批首个、最后一个贡献非空增量正文的模型事件。按 seq 拼接每个事件贡献的完整正文；工具或元数据事件不贡献文字。单个事件不可拆分，不跳过中间正文片段 |
| `display` | 从原模型请求 seq 到成功终态 seq。重建并完整校验响应，按模型 I/O 规则选出的最终正文或拒答整体替换显示；不要求先存在 `attempt.accept` |

展示动作键见第 2 节。同一尝试的普通批次按序覆盖完整片段，范围不重叠、不漏正文；最终替换允许覆盖已经临时展示的来源。
恢复保留已记录的范围和用途，不重新合批。正文从模型源事件重建，不复制到 WAL。

先确认意图，再发布业务事件；先确认业务 seq 和 `effect.commit`，再执行依赖动作。

## 6. 工具意图、结果和回复关闭

`tool.prepare.data`：

- `tool_key`、`task_id`、`answer`、`call_id`、`name`、`arguments`：稳定任务标识、直接归属和已完整校验的模型参数。
- `request`：exec 保存解析后的固定 `workdir` 和 payload `ts_ms`；`command`、可选 `timeout_ms` 从同记录的 `arguments` 取得。read 保存 `ts_ms`、`target_seq`、`stream` 和起始 `offset`，`nbytes` 按页加入副作用意图。
- `output_limit`、`chunk_bytes`、`nbytes`：输出额度、分页大小、模型要求的总读取量，固定结果解释。

`task_id` 只生成一次，后续工具事件及结果呈现沿用。真正 `exec_id` 取已提交 Cmd run request 的 seq，不能用 task_id 或预分配值代替。
exec 的附件 ID 只保存在 `arguments.object_ids`，按首次出现顺序去重；省略或空数组表示不携带附件。
完整 ObjectKey 凭据由源 Chat 事件重建，不写入 WAL。工具准备不保存第二份 OpenEvent 消息 UUID。

`tool.done.data` 必含 `tool_key` 和 `outcome`：

| outcome | 其他字段 |
| --- | --- |
| `exec` | `request_seq`、`result_seq`；可选 `rejection`，仅允许 `result_too_large` |
| `read` | 第一页 `request_seq`、正整数 `page_count`、最后一页 `result_seq`；可选 `rejection`，仅允许 `result_too_large` |
| `read_cancelled` | 第一页 `request_seq`；已有完成页、取消后不再发下一页且没有在途读取请求 |
| `timeout` | `request_seq` |
| `cancelled` | 无其他字段，只用于尚未发布的工具 |

逐页请求提交记录和 Cmd 结果索引重建读取结果；首请求、页数和末结果须与裁决一致，不保存逐页结果数组。
分页偏移按每个成功 Cmd 结果回显的 `nbytes` 推进。取消或超时结果不保存不参与结果构造的页列表。
接受普通模型输出前，工具准备、发送及完成记录的大小都须检查。

完整结果不能通过模型历史准入时，仍按真实来源保存 `exec` 或 `read`，同时固定 `rejection="result_too_large"`。
该裁决只拒绝模型可见正文呈现，模型输入改用 [I/O 的固定短错误](MODEL_IO_cn.md#61-大工具结果的拒绝呈现)，
真实 exec_id、退出码、字节计数及读取位置仍从原记录取得；不改命令成败、不删除原 Cmd 结果、不重跑命令。
不复制正文或错误文本到 WAL，不新增压缩尝试；恢复沿用原拒绝呈现，不因当前预算变大重新判定。

确认的 `tool.done` 是唯一模型结果，恢复直接复用，迟到结果不能覆盖。
结果正文由源事件重建；没有实际 run result 的超时或未发取消不提供虚构 exec_id。

`tool.done` 是业务结果裁决，不是实际执行结束证明；实际执行与清理仍由原 Cmd 请求及真实终态确定，
并保留原回答归属。取消、FIFO 推进、截止时间裁决和回复关闭规则见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)。

回复收尾不写单独的完成记录。除第 3 节尚未创建正常回复的本地失败外，完成状态由所属模型阶段的裁决或已确认取消、
原 Chat 回复的实际获胜 end/cancel、取消所需的 Model 流终态、全部直接工具的唯一结果与已发布工具请求的 Cmd 终态，
以及必要的在途发布核对和实际执行清理共同推导。
只有 Chat 终态或只有工具结果都不能代替其余事实；恢复沿相同来源继续未完成工作，不触发额外模型续问。
副作用仍保留 `effect.prepare` 与 `effect.commit`，按原意图和提交来源核对，不另存一份回复终态。

## 7. 停机和恢复

停机与重启前提见 [Agent 停止、重启和排错](CHAT_MODEL_AGENT_cn.md#停止重启和排错)，进程控制步骤见
[Runtime 启动、更新与停止](RUNTIME_RECONCILER_cn.md#启动更新与停止)。
WAL 不另写停机汇总；恢复事实来自本协议规定的记录及原始事件，未决动作保留原键、来源和已知发布证据。
