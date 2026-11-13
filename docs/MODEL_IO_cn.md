# Chat Agent 模型输入输出格式 v3

[English version](MODEL_IO.md)

> 实现状态：模型 I/O v3 的事件输入、输出解析、异步工具和上下文压缩已实现；测试范围与外部联调限制见 [README](../README_cn.md)。
> 模型 I/O 版本：`3`。
> 日期：2026-10-27
> 范围：模型可见事件、历史摘要及任务信息、三项工具和流式响应解释。

## 1. 文档边界

本文面向提示词作者、模型接入者和 Agent 使用者，是模型可见输入、工具参数与结果、响应解释的唯一格式说明。
配置、字节额度、会话与失败后的使用行为见 [Chat Agent](CHAT_MODEL_AGENT_cn.md)；
WAL 字段、稳定键、事件来源、排序游标和压缩检查点见 [WAL 协议](AGENT_WAL_PROTOCOL_cn.md)；
部署、对象读取环境、工作目录及提示词文件入口见 [Runtime](RUNTIME_RECONCILER_cn.md)。
本文不定义另一套调度、持久化或恢复流程。
`llm.v1`、`chat.v1`、`cmd.v3` 的原始协议由各模块维护，本文只定义 Agent 如何解释和提供其中的事实。

模型只获得文本、附件元信息、可关联的任务与回答标识和工具结果。Channel、principal、token、完整 ObjectKey、
WAL 键、重试与恢复状态、OpenEvent 消息 UUID、模型请求 ID 不进入输入。
`answer_id` 是原回答 `turn_id` 的 UUID 字符串，只用于明确回答归属；`task_id` 是工具准备时固定的 UUID 字符串，
只用于关联原调用、摘要中的任务信息和最终结果，两者都不是 WAL 键。数字 `exec_id` 用于读取命令输出，数字 `object_id` 用于读取附件。
数字 `input_id` 复用原完整用户 `turn.single` 的正整数 seq，只用于关联用户请求与针对它的回答取消。
这些标识不能互换，也不包含读取凭据；不因此向模型提供其他内部事件序号或恢复游标。

## 2. 请求与完整提示词

普通调用使用 Model Proxy 的 `POST /v1/chat/completions`，保持 `stream=true`、`n=1`，
`body.tools` 使用第 5 节的三项定义。`body.messages` 固定由两条原生消息组成：

1. `role=system`，`content` 是该次输入冻结的完整系统提示词。
2. `role=user`，`content` 是第 3 节对象的 JSON 序列化字符串，承载按来源排列的会话事实。

模型**输出**仍使用 Chat Completions 原生 `content`、`refusal` 和 `tool_calls`，供 Agent 完整解析、校验和分派。
下一次**输入**把已接受的输出及工具事实转换为第 3 节事件，不回放原生 `assistant.tool_calls` 或 `role=tool` 消息。
不生成“已接收”“排队中”或 `accepted/pending/queued` 工具回执；工具调用事件本身记录真实调用，结果在实际确定后另行进入事件序列。
输入中的 `role=user` 是事件容器，只有 `type=user` 是用户发来的消息；其他类型保持各自来源，不伪装成用户指令。

两份完整提示词的加载、配置和冻结方式遵循[提示词配置](RUNTIME_RECONCILER_cn.md#提示词文件)；
系统文件包含业务与本协议的使用说明，Agent 不再隐式拼接另一段固定提示词。
仓库提供完整的[默认系统提示词](../openevent-stack/prompts/system.txt)和
[默认压缩提示词](../openevent-stack/prompts/context_compression.txt)，可以直接作为配置的文件入口；
修改业务内容时仍须保留本协议要求的事件、摘要、工具和取消解释。提示词是本协议的使用文本，不另定义协议字段。
工具用途和参数由工具定义说明；系统提示词补充上下文及结果的理解方式，不重复参数清单或程序内部校验流程。
模型名和 Provider 由部署配置提供，Provider 凭据不进入模型消息；不增加 tokenizer、输出 token 参数或 Provider 专用参数。
请求字节计量、普通与压缩请求的额度统一见[上下文字节预算](CHAT_MODEL_AGENT_cn.md#上下文字节预算)。

一份模型输入准备完成后保持不变，失败重试沿用相同内容；期间到达的事件留给下一份输入。
同会话工具在后台执行期间，新用户消息可以形成下一次模型请求，触发条件见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)。
Agent 自己生成的处理占位、工具结束提示、失败通知和消息过长通知属于 Chat 展示文字，不作为模型生成的 `assistant` 事件；用户显式引用时，按回复引用规则取得最终可见内容。
按 [Agent 过长消息规则](CHAT_MODEL_AGENT_cn.md#过长用户消息)持久拒绝的用户输入不生成独立 `user` 事件，
不进入后续压缩材料。后来用户主动引用它时，仍按第 3.2 节作为新输入的 `reply_context` 完整计量和选入，
不恢复原独立事件；引用附件也只从这次新输入取得来源。

## 3. 统一输入结构

`role=user.content` 解码后恰好包含以下两个字段，不允许其他顶层字段：

| 字段 | 格式与含义 |
| --- | --- |
| `summary` | 无历史摘要时为 null；否则为第 4.2 节的摘要正文字符串 |
| `events` | 非空数组，包含本次保留的未被历史摘要覆盖的旧事件和选入的新事件，按源事件顺序排列 |

历史摘要在前，随后是其覆盖范围之后的连续模型事件；不能跳过中间事件改选后面的消息。
已经持久拒绝的源消息不属于模型事件，范围覆盖依据见 [WAL 协议](AGENT_WAL_PROTOCOL_cn.md#4-事件引用顺序与压缩检查点)。
输入选择与会话行为见 Agent 文档，压缩覆盖范围及固定水位见 WAL 协议；模型按 `events` 数组顺序理解，不按 `time` 重新排序。
每个事件都有 `type` 和 ISO8601 UTC 字符串 `time`，其余字段只允许下表列出的字段。

### 3.1 事件字段与来源

| `type` | 其他必填字段 | 可选字段 |
| --- | --- | --- |
| `user` | `input_id`：原用户 single 的正整数 seq；`text`：字符串；`attachments`：附件数组 | 非空 `reply_context` 数组 |
| `assistant` | `answer_id`：原回答 UUID；`text`：模型正文字符串 | `refusal`：非空拒答原文 |
| `tool_call` | `answer_id`；`task_id`；`name`：三项工具之一；`arguments`：校验后的参数 object | 无 |
| `tool_result` | `task_id`；`result`：第 6 节的单个结果 object | 无 |
| `user_cancelled` | `target_answer_id`：用户取消的原回答 UUID；`target_input_ids`：该回答针对的用户请求 ID 数组 | 无 |

- `user` 来自配置用户发送的一条完整 `turn.single`，其 `input_id`、顺序和 `time` 都取这条源事件。
  重复进入后续上下文、压缩与恢复时沿用同一 `input_id`，不额外分配或持久维护另一套输入 ID。
- `assistant` 只来自 Agent 已经完整接受的普通模型输出，顺序与 `time` 取该输出的模型终态源事件。
  `text` 保存聚合的 `content` 原文，只有拒答时使用空字符串；同时存在正文和拒答时分别保存。
  只有工具调用、没有正文或拒答的输出不生成空 `assistant` 文本事件。
- `tool_call` 来自上述已接受输出，使用同一个模型终态来源和时间；同源的 `assistant` 文本在前，
  调用按模型数组 index 顺序排列。`task_id` 从相应工具准备记录取得，`arguments` 使用校验后的 object，
  保留命令与路径字符串的原值，不把原生 arguments 的 JSON 包装字符串再嵌套一层。
  这个事件记录已通过校验的模型调用，是否成功执行由之后的 `tool_result` 表达。
- `tool_result` 用 `task_id` 关联原调用，不要求与该调用相邻。真实结果的顺序和 `time` 取形成完整结果的最后必要源事件；
  本地超时、未发取消等裁决按 WAL 的来源规则定位。保存裁决或拒绝呈现不会把真实结果改排到更晚的位置。
- `user_cancelled.time` 取真实用户取消源事件的时间；其排列位置及确认规则以 WAL 为准。
  `target_answer_id` 明确指定被取消的回答，`target_input_ids` 原样采用该回答准备时冻结的 `reply_to_seqs`，
  按原顺序列出对应用户事件的 `input_id`，不根据后来消息重新推导，也不靠相邻位置猜测目标。
  该数组非空，元素为不同的正整数；它说明原回答针对哪些请求，不扩大对其他回答或工具的取消范围。

同一 `task_id` 只对应一项原始工具调用和一项最终结果；重试、恢复及压缩不重新分配任务标识。
Provider 原生 tool call ID 仍用于输出解析和源记录核对；输入回放用稳定 `task_id` 关联，不要求 Provider 跨回答复用原生 call ID。
隐藏 reasoning、Provider 元数据和未被完整接受的半截输出不生成回放事件。

### 3.2 用户正文、附件和回复引用

本 Agent 只处理完整用户 `turn.single`。`text` 按合法 part 顺序连接，不添加分隔符、不裁剪空白；
`attachments` 保持源消息携带顺序。`text` 可以为空，纯附件输入仍是有效用户消息；空正文且无附件的消息不构成新用户输入。
附件信息只属于引用它的这条消息；原消息保留在历史中时随原消息提供，不复制到后续无关消息。

每个附件恰好包含 `name`、`type`、`nbytes`、`object_id`：前三项来自对象元信息，`object_id` 是源 ObjectKey 中的正整数 ID。
提交模型输入前必须准备元信息和内部 ObjectKey 映射，不提前下载正文，也不能因准备失败静默去掉附件。
附件不作为视觉、音频等原生多模态输入发送；模型按第 3.3 节读取。
媒体类型缺失时使用 `application/octet-stream`，不从文件名猜测；对象读取环境见 Runtime 文档。模型输入不包含对象下载凭据、完整 ObjectKey、缓存路径或外部存储地址。

用户消息带回复引用时，`reply_context` 提供目标最终可见内容，不暴露引用 seq：

```json
{"reply_context":[{"role":"assistant","status":"completed","text":"日志检查完成。","attachments":[]}]}
```

- 无回复时省略；非空时按用户消息的回复列表顺序排列，不递归展开目标的回复。
- 每项恰好包含 `role`、`status`、`text`、`attachments`。`role` 由 Agent 映射为 `user` 或 `assistant`，
  `status` 固定为 `completed`。回复目标按 Chat 协议的有效正常结束位置读取，取消目标不能被回复。
- `text`、`attachments` 使用本节相同规则；目标在用户消息创建前已经正常结束，内容和终态不再变化；
  Chat 错误结束的最终正文和附件为空。
- 引用附件也准备元信息与内部映射；引用内容是该用户消息的上下文，不另算新输入。

### 3.3 通过 Cmd 读取附件对象

应用命令 `openevent-read-object <object_id>` 由模型通过已有 `exec` 调用，不增加第四项工具或新的 Cmd 动作。
命令由本项目安装，运行环境由 Runtime 配置。

- 参数是附件中的十进制 `object_id`，并且在本次 `exec.object_ids` 中选择它。
  Agent 只随请求传递所选附件凭据；命令从环境取得 ObjectKey 和对象服务地址，模型不用填写凭据或服务地址。
- 成功时向 stdout 写完整原始对象字节并以 0 退出，不附加文件名、状态文字或编码包装；
  工具层按第 6 节编码和限制模型可见输出。
- 对象完整读取成功后才写 stdout；引用无法解析或对象读取失败时非 0 退出，stderr 给出不含凭据的原因。
  stdout 写出失败也以非 0 退出；必须结合退出状态判断是否取得完整对象。
- 每次读取都来自不可变对象。重定向获得的工作副本可编辑，修改副本不改变之后按同一 ID 读取的原内容。
- 继续读取已生成的命令输出时使用真实 `exec_result.exec_id` 调用 `read_stdout`，无需重新下载。

```json
{"command":"openevent-read-object 12345","object_ids":[12345]}
```

对象引用与凭据传递见 [Agent 附件说明](CHAT_MODEL_AGENT_cn.md#模型工具与附件)，工作目录见 Runtime 文档；通用 shell 的正常文件访问能力不等于附件隔离。

### 3.4 连续两次输入

完整 OpenAI-compatible 请求见[第一次输入](examples/context-input-1.json)和[第二次输入](examples/context-input-2.json)。
文件包含完整 `messages`、JSON 字符串形式的 `user.content` 和三项工具定义，示例内容以这些文件为准。

第一次输入保留用户 A、已接受的工具调用 T，随后包含工具执行期间到达的用户 B。
A 的消息携带两个附件，B 的无关消息不会继承它们。回答 A 只有工具调用，没有正文，因此不生成空 assistant 文本事件。
第二次输入保留相同历史前缀，再追加针对 B 的解释和工具 T 的真实结果；两次之间没有接收回执，也没有需要补齐的原生工具历史消息。

同一段历史的[压缩请求](examples/context-compaction.json)只选择完整旧事件；[带摘要的普通请求](examples/context-with-summary.json)
展示压缩模型保留仍有用的附件信息、丢弃已过时附件信息的效果。任务信息按第 4.1 节写入摘要正文。

## 4. 历史摘要与任务信息

上下文使用“历史摘要＋其后原始事件”的形式。摘要正文概括仍有用的任务信息，不再附带完整工具调用表。
历史压缩可以覆盖完整的连续旧事件前缀，包括结果尚未到达的工具调用。已经完整确认的输出可以直接压缩，不必先放进一次普通模型输入。
尚待处理的用户消息和正常工具结果保留原事件，不提前用摘要替代；具体选择边界见下述 WAL 协议。
每批可以在两个完整事件之间结束，包括同一已接受输出生成的 assistant 与 tool_call 之间；一个调用及其结果不必同时处于可压缩范围。
压缩不修改原事件顺序，不删除原始事件，也不改变已经固定的模型输入。
压缩的来源范围与提交边界见 [WAL 协议](AGENT_WAL_PROTOCOL_cn.md#4-事件引用顺序与压缩检查点)；字节预算、固定阈值和失败后的使用行为见
[Agent 配置](CHAT_MODEL_AGENT_cn.md#上下文字节预算)。

### 4.1 摘要中的任务信息

压缩模型在摘要正文中保留未完成任务的原 `task_id`、任务目的和后续处理需要的信息；
涉及用户请求、回答归属或取消时，保留相应的原标识及明确范围。后续 `tool_result` 按原顺序进入 `events`，通过 `task_id` 关联。
摘要描述其固定截止位置上的事实；后续事件可能已更新任务结果，不能仅凭旧摘要还写着“等待结果”就认定任务仍在执行。

命令和参数可以概括为任务目的及必要细节，不要求把完整命令、全部参数或所有历史调用重新列一遍。
仍需保留的标识和具体参数值必须忠实于来源，不能编造；已经结束且对后续无用的细节可以省略。
尚未被摘要覆盖的 `tool_call` 事件仍完整保存原参数，不能在原事件内截短；进入摘要后的表述遵循本节。

工具的真实参数、归属、执行和清理状态始终从原工具、Cmd 及取消记录重建，程序不从摘要正文恢复执行状态。
摘要省略参数不修改执行记录，不会重新执行、取消或改变已有调用。模型侧不再设置一份必须完整重传的调用附表。

### 4.2 摘要格式与压缩调用

普通输入与压缩输入的 `summary` 使用同一格式：没有历史摘要时为 null，否则为非空白的摘要正文字符串，不使用对象包装。
例如摘要已经覆盖一次调用，但还没有覆盖它的结果时：

```json
"用户请求 301 要检查 app.py 是否可读；回答 10000000-0000-4000-8000-000000000001 已发起任务 20000000-0000-4000-8000-000000000001 执行该检查，截至本摘要尚无结果。"
```

摘要正文须已被确认采用，围绕目标、约束、已完成事实和待办事项组织，不把计划或工具调用改写为成功结果。
附件元信息只放在引用附件的原用户事件或回复引用中，不复制到后续无关消息。
压缩模型根据任务需要，自行决定在摘要正文中保留哪些附件的 `object_id`、名称或用途，也可以丢弃不再需要的信息。
摘要不另设附件字段，程序不回填全历史附件目录；工具需要的真实附件凭据仍沿第 5 节规定的原消息来源取得。
摘要不包含凭据、WAL 键或内部执行状态。任务 ID、参数、取消目标与执行裁决仍以精确来源为准。

完整压缩提示词必须要求保留用户目标与约束、已经确认的结果、未完成工作及对应 `task_id`，以及用户取消的明确目标。
仍待处理的用户请求及取消涉及的请求须保留对应 `input_id`，不能丢失或改写这些关联。
尚无结果的工具调用和计划只能写成待完成事项，不能写成已经成功；任务标识不得改写，参数可按第 4.1 节概括。
压缩模型只总结提供的材料，不继续执行用户任务、不提出新的工具调用，只输出摘要正文，并遵守本次 `max_summary_bytes` 明确给出的大小目标。
提示词还须要求把已采用的摘要与本次完整事件结合起来；某项未完成工作没有在这批新事件中再次出现，不代表它已经结束。
这些文字要求不替代程序保留的原始执行记录及裁决，也不要求程序在摘要之外补回全部参数。

压缩调用沿用普通调用的模型、Provider、接口和 `stream=true`、`n=1`，`messages` 只有完整压缩提示词的 system 消息
和一条 user 消息，不传 `tools`。其 user.content 解码后恰好包含：

```json
{"max_summary_bytes":16384,"summary":null,"events":[{"type":"user","input_id":301,"time":"2026-10-27T08:00:00Z","text":"检查 app.py 是否可读","attachments":[]}]}
```

`max_summary_bytes` 是正整数，明确指定本次摘要正文的硬上限；上例的 `16384` 只用于展示格式，实际值按公开预算规则计算。
其计量为摘要正文替换空串后，完整请求 JSON 按该规则计算的字节增量，包含 user.content 与外层 JSON 的转义，
不等于原始 UTF-8 字节、字符数或 token 数；准确计量由 Agent 执行，模型应按此目标尽量简洁地输出。
`summary` 为已经采用的历史摘要正文或 null；`events` 是紧接其覆盖范围之后、本次选定的完整连续历史事件前缀，保持原顺序。
每次请求只选能装下的前缀，不切开单个事件或 JSON 字符串，也不单独压缩一个工具结果的正文。
一份完整摘要被接受后立即成为该前缀的检查点，至少推进一个完整事件；不要求每一批相对前一批都变短。
剩余历史需要继续压缩时，使用这份摘要和下一段完整事件。
没有单独等待“所有片段结束”才可采用的中间摘要。来源范围与恢复检查点由 WAL 协议定义。
单条事件准入、压缩后的历史硬目标和实际请求检查见[上下文字节预算](CHAT_MODEL_AGENT_cn.md#上下文字节预算)。
只有摘要与尚未压缩的旧事件整体达到该目标，才算为下一条合规输入腾够空间；不能只检查摘要正文变短了。

压缩输出只接受 `finish_reason="stop"` 的非空白正文字符串，不能带工具调用或非空 refusal；正文直接作为摘要，
不要求模型另写附件目录或任务状态。它不进入 Chat 展示，也不生成普通 `assistant`、`tool_call` 事件。
摘要超过本次 `max_summary_bytes` 时整次尝试失败，不截短后当作有效摘要。接受后的正文及其来源由 WAL 引用，恢复不重新选择一份不同摘要。
新用户原文不先做摘要；过大消息按 Agent 准入规则拒绝，大工具结果使用第 6.1 节的短错误呈现。

### 4.3 取消与失败事实

用户取消生成带明确 `target_answer_id` 和 `target_input_ids` 的 `user_cancelled` 事件，不依赖前后相邻消息或旧原生消息组表示目标。
若取消前还没有完整接受模型输出，该目标可以没有对应的 `assistant` 文本事件，仍可通过 `target_input_ids` 确定原回答针对的用户请求，
不能为补齐历史伪造一条回答。仅工具结果触发的回答也使用其原先冻结的回复目标，不把期间的新用户消息加入取消范围。
Agent 自己因失败发布的 cancel 不生成用户取消事件；模型看过取消事实后不能自动继续被取消回答的任务。

取消不撤销已经接受的模型事实，也不改写已经确定的工具结果。尚未派发的工具使用真实的未发取消结果；
已派发请求使用已经确认的实际结果或超时裁决，分块读取提前取消也保留已开始的事实。
发送 kill 不能直接解释为任务已终止。相关工具的唯一结果仍以 `tool_result` 保留，不能为了凑出配对而补造回执。
取消的执行范围、结果触发条件和新用户消息的处理见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)。

普通模型调用最终失败时，保留固定输入和此前已接受的事实；未接受的半截输出不进入回放。
失败通知只是 Chat 展示，不新增用户任务或成功回答。摘要涉及取消时须保留其明确目标与已停止事实；
摘要不能撤销取消、改变任务归属或把已经发生的命令效果描述为已回滚。

## 5. 三项工具 schema

工具名称固定为 `exec`、`read_stdout`、`read_stderr`，不增加模型侧取消工具。
两个输出读取工具支持显式指定字节范围，不维护隐式分页游标。
发给模型的 schema 省略 `maximum`；Agent 接受工具调用前仍按下文的 uint64 范围校验。
每个函数参数 object 都拒绝未知字段；JSON integer 不包括 boolean。

```json
[
  {
    "type": "function",
    "function": {
      "name": "exec",
      "description": "通过 Cmd 执行非交互式 shell 命令，实际结果随后作为 tool_result 事件提供。默认使用当前会话工作目录。不要用来发送 Chat 消息，也不要请求 stdin、交互程序或后台常驻任务。",
      "parameters": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "command": {"type": "string", "minLength": 1, "description": "完整命令行，不含解释文字。"},
          "workdir": {"type": "string", "minLength": 1, "description": "可选的绝对工作目录。省略时使用会话工作目录。"},
          "timeout_ms": {"type": "integer", "minimum": 1, "description": "可选命令超时，单位毫秒。"},
          "object_ids": {
            "type": "array", "default": [],
            "items": {"type": "integer", "minimum": 1},
            "description": "只列出本次命令必须访问、且在当前输入中出现的附件 ID。最多 1024 个不同 ID；重复 ID 按首次出现顺序去重。省略或空数组表示不携带附件。"
          }
        },
        "required": ["command"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "read_stdout",
      "description": "读取已有 exec_result 对应的 stdout 字节范围，实际结果随后作为 tool_result 事件提供。exec_id 必须取自该结果；继续读取请显式推进 offset，相同参数重复调用仍读取相同范围。",
      "parameters": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "exec_id": {"type": "integer", "minimum": 1},
          "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "起始字节位置，省略时从头读取。"},
          "nbytes": {"type": "integer", "minimum": 1, "description": "必填的原始字节数；成功时精确读取本值与输出额度中较小的字节数，所读范围必须存在。"}
        },
        "required": ["exec_id", "nbytes"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "read_stderr",
      "description": "读取已有 exec_result 对应的 stderr 字节范围，实际结果随后作为 tool_result 事件提供。exec_id 必须取自该结果；继续读取请显式推进 offset，相同参数重复调用仍读取相同范围。",
      "parameters": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "exec_id": {"type": "integer", "minimum": 1},
          "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "起始字节位置，省略时从头读取。"},
          "nbytes": {"type": "integer", "minimum": 1, "description": "必填的原始字节数；成功时精确读取本值与输出额度中较小的字节数，所读范围必须存在。"}
        },
        "required": ["exec_id", "nbytes"]
      }
    }
  }
]
```

`exec` 省略 `workdir` 时，Agent 向 Cmd 显式传入当前会话 workdir，不依赖 Cmd worker 的默认目录。
工具名与参数严格按上述 schema 校验；`workdir` 还必须满足绝对路径语义。
`timeout_ms` 和 `exec_id` 都限制在 Cmd 所接受的正 uint64 范围，不接受超界整数后再尝试发布。
`exec.object_ids` 省略或为空数组时不携带附件；非空时按首次出现顺序去重，最多保留 1024 个不同的正 uint64 ID。
每个 ID 必须有本次冻结输入范围内的原始附件来源：未压缩用户事件及其回复引用，或已采用摘要所覆盖的原用户事件及其回复引用。
Agent 从这些原消息的 ObjectKey 取得凭据，不把摘要正文或其它文字中提到的 ID 当成凭据。
压缩只决定模型还能看到哪些附件信息，不撤销已有原始引用；程序不因此把全部历史附件再次列入模型输入。
不按 shell 文本猜测附件，不自动附带全部输入附件，也不纳入模型请求后新到或其他会话消息的附件。
Agent 接受整份 assistant 前校验全部工具的选择；类型、数量或来源不合法时，沿第 7 节的模型输出失败规则处理，
整份 assistant 不接受，任何工具都不执行。两个输出读取工具不接受 `object_ids`，也不携带附件。
`offset` 必须是 0 到 18446744073709551615（uint64 最大值）之间的整数，默认 0；
`nbytes` 必须是 1 到同一最大值之间的整数，为必填项，没有默认值。超出范围的参数在接受工具调用前拒绝。
Agent 在记录工具操作意图时补齐 `offset` 并冻结 `max_tool_output_bytes`；恢复同一操作不因配置变化改写读取范围或输出额度。
输出读取的目标关系和权限由 Cmd 契约处理，Agent 不复制 Cmd 授权规则。

## 6. 模型可见工具结果

每个 `tool_result` 事件的 `result` 是以下单个结果 object，不是 JSON 字符串或结果数组。
事件通过稳定 `task_id` 关联原调用，不发送原生 tool 消息或任务接收回执。
先按本节定义形成完整原始结果；未通过 Agent 准入检查的大结果使用第 6.1 节的拒绝呈现。
通用必填字段为 `type` 和 `status`；`status` 只允许 `ok` 或 `error`。
错误结果必须有非空 `error_message`，需要机器可识别类别时使用 `error_code`。
本文定义 `timeout`、`cancelled` 和 `result_too_large` 错误码，其余错误使用可读说明，不猜测执行状态。

| `type` | 专用字段 |
| --- | --- |
| `exec_result` | 已接受 Cmd run result 时必填 `exec_id`、`stdout_bytes`、`stderr_bytes`；原 result 为 `SUCCESS` 或 `FAILED` 时必填真实 `exit_code`；可含 `stdout`、`stderr` |
| `read_stdout_result` | 必填原调用的 `exec_id`、实际采用的 `offset` 和 `nbytes`；成功时必填 `stdout` 和 `eof` |
| `read_stderr_result` | 必填原调用的 `exec_id`、实际采用的 `offset` 和 `nbytes`；成功时必填 `stderr` 和 `eof` |

`exec_result` 不携带 `command`，正常、失败、超时和取消结果使用同一规则。
完整命令由 `task_id` 对应的 `tool_call.arguments` 和原始任务记录保留，不在结果中重复回显；
调用被摘要覆盖后，由摘要正文保留任务关联和必要信息，完整参数仍保存在原执行记录中，不随每次普通输入重新附带。

`exec_id` 是原始 `cmd.run.request` 的正整数 seq，不是结果 seq、字符串或输出文件路径。
`stdout_bytes` 和 `stderr_bytes` 是 Cmd 生成原 run result 时观察并记录的两个流的非负总字节数，长输出没有内联内容时也必须提供。
模型可据此选择读取范围；这些计数不限制后来实际读取的范围，也不保证输出文件此后不会变化。
等待 run result 超时或未执行即取消时不提供 `exec_id` 或这两个计数，并明确禁止继续读取该次执行输出。
Cmd 返回真实 `TIMEOUT` 或 `KILLED` run result 时仍可提供 `exec_id`，分别用 timeout 或 cancelled 表达原因。

已接受的 Cmd run result 按下表转换；Cmd 状态本身的含义以公开 Cmd 协议为准：

| Cmd `status` | 模型结果 `status` | `exit_code` | 错误表达 |
| --- | --- | --- | --- |
| `SUCCESS` | `ok` | 原值 0 | 不生成错误说明 |
| `FAILED` | `error` | 原非零整数 | `error_message` 必须说明实际退出码，保留已有错误原因 |
| `TIMEOUT` | `error` | 不提供 | `error_code="timeout"`，说明 Cmd 报告超时 |
| `KILLED` | `error` | 不提供 | `error_code="cancelled"`，说明 Cmd 报告任务被终止 |
| `START_FAILED` | `error` | 不提供 | 说明命令未启动并保留原错误原因 |
| `RECOVERY_FAILED` | `error` | 不提供 | 保留 Cmd 的恢复失败原因，不推断命令从未产生效果 |

`exit_code` 只转交真实结果中的整数，不把缺失值补成 0，也不以 `error_message` 代替已有退出码。
非零退出码不自动触发重新执行命令；模型可以据实际退出码判断命令的业务含义。

Agent 自己等待工具结果超时后，按已确认的超时裁决生成 `status="error"`、`error_code="timeout"`
和明确的超时说明，不提供尚未取得的退出码；等待 run result 超时也不提供 `exec_id`。
这次调用按失败结束。迟到的结果不修改该失败、不补 `exec_id` 或第二个 `tool_result` 事件，
不为这次失败新增后续结果查询入口。超时结果不能伪称已收到 Cmd 的真实 `TIMEOUT` 或 `KILLED`。
单次 RPC 失败的重试和进程退出见 Agent 文档，不与这里的工具结果等待超时混用。

结果出现 `stdout` 或 `stderr` 时，其值必须是下面这种编码明确的 object：

```json
{"encoding":"utf-8","content":"hello\n","bytes":6,"truncated":false}
```

- `encoding` 只允许 `utf-8` 或 `base64`；`content` 是字符串，`bytes` 是其代表的原始字节数。
- `utf-8` 的 content 是解码后的文本；`base64` 的 content 是原始字节编码，不能直接冒充命令文字。
- `truncated` 是必填 boolean，表示是否因模型可见输出额度，只返回了本次请求范围的前缀。
  `exec_result` 的内联输出按从 0 开始的完整输出处理；范围读取按下面的规则判断，不能把 `truncated=false`
  当成已经读取整个文件。
- 空输出使用 utf-8、空字符串、0 bytes 和 `truncated=false`；输出字段缺失表示本次没有内联该流。
- `exec_result.stdout.bytes` 和 `exec_result.stderr.bytes` 只计算实际内联的内容，受输出额度限制；
  它们不替代原 run result 的 `stdout_bytes` 和 `stderr_bytes` 总量。
- stdout/stderr 独立处理；不得给模型暴露 Cmd 输出文件的内部路径。

`read_stdout`/`read_stderr` 从指定 `offset` 开始，成功时精确返回 `min(nbytes, max_tool_output_bytes)` 个原始字节。
Agent 按配置的分块大小构造 Cmd 精确范围读取请求，后续位置按成功返回的字节数推进，直到读满本次允许的范围；
任何一页失败都使整次工具读取失败，不把此前已读的前缀当成成功结果。Agent 不按历史输出计数裁剪读取范围，
也不在本地推断 EOF；空文件、起点到达或超过 EOF，以及有效范围越过 EOF，都按 Cmd 的实际结果返回工具错误。

成功结果的 `offset` 和 `nbytes` 回显补齐 `offset` 默认值后的模型请求范围；`stdout.bytes` 或 `stderr.bytes` 是本次
实际返回量。下一次读取位置为 `offset + bytes`，不用请求量替代实际量。相同参数重复调用不隐式推进游标。
`eof` 为 boolean，采用本次最后一页成功 Cmd 读取观察到的 EOF；它不保证之后不会有新的输出追加。

- 已读满请求的 `nbytes`，但文件还有后续内容：`truncated=false`、`eof=false`，模型可以继续指定下一范围。
- 请求的 `nbytes` 超过输出额度时，成功结果为 `truncated=true`；此时最后一页恰好到 EOF，`eof` 也可以为 true。
- 请求的 `nbytes` 不超过输出额度且恰好读到 EOF：`truncated=false`、`eof=true`。
- 文件不存在或读取失败映射为工具错误，不能因为 Cmd 错误结果带有 `eof=true` 就伪装成成功空输出。

各页先还原为原始字节再顺序合并，不能直接拼接 base64 字符串；UTF-8 字符跨页时也必须先合并再解码。
请求的起止位置落在 UTF-8 字符内部时不调整范围，所取字节无法完整解码就使用 base64，不丢字节或静默替换。

例如 `read_stdout(exec_id=123, offset=65536, nbytes=4096)` 读取该次执行从字节偏移 65536 处开始的一段输出。
若实际返回 4096 bytes 且 `eof=false`，下一段从 69632 开始读取，无需重新执行原命令。

### 6.1 大工具结果的拒绝呈现

完整工具结果未通过 [Agent 的准入检查](CHAT_MODEL_AGENT_cn.md#上下文字节预算)时，不把过大正文加入模型历史，
不另外调用模型压缩它，也不截短后冒充完整结果。改为固定的短错误呈现，事件仍保留原 `task_id`、来源、时间和排序位置。
例如命令实际成功，但输出过长时：

```json
{
  "type":"exec_result",
  "status":"error",
  "error_code":"result_too_large",
  "error_message":"Tool result is too large to provide. Request a smaller range.",
  "exec_id":701,
  "exit_code":0,
  "stdout_bytes":65536,
  "stderr_bytes":0
}
```

- `status="error"` 和 `result_too_large` 表示这次结果无法完整提供给模型，不表示 Cmd 执行失败；真实退出码保持原值。
- 省略 stdout/stderr 正文对象以及过大的原错误文字。固定 `error_message` 如上；已经取得的真实 `exec_id`、`exit_code`、
  `stdout_bytes`、`stderr_bytes`，以及读取结果中的 `offset`、`nbytes`、`eof` 等必要短字段保持原值；没有取得的事实不能补造。
- 原 Cmd 数据继续保留；不重跑命令、不重读文件来替换原结果。需要准确正文时，模型可按真实 `exec_id` 请求较小范围。
- 短错误本身也必须通过准入检查；有上界的字段在启动基线中预留，依赖调用参数的字段在接受工具前按真实参数检查。呈现选择按 WAL 保存，恢复不重新选择表示；
  记录该选择不改变真实结果在事件流中的位置。

## 7. 流式聚合与完整校验

Agent 先按 `llm.v1` 接受当前调用的合法消息，再解释 Provider body；协议终态和模型业务校验分开。
每条 append 的 body 必须是当前适配范围的 Chat Completions chunk object。
`choices` 为空的 usage 等元数据片段不产生文字或工具；非空时只处理唯一的 choice 0。
不将其他 choice、隐藏 reasoning 或未在本文定义的响应字段转发到 Chat 或输入事件。
正文、拒答、工具 ID 和 arguments 字符串必须能编码为 UTF-8；arguments 解析后的命令与路径也按此校验。
不能编码的字符（例如 JSON 转义产生的孤立代理码点）按模型输出失败处理，不替换字符后继续执行。
流式文本在进入展示队列前校验，完整响应在接受 assistant 和执行工具前校验。
完整响应归一化后的每个完整事件还必须通过 [Agent 准入检查](CHAT_MODEL_AGENT_cn.md#上下文字节预算)；任一事件超限则整次不接受，任何工具都不执行，
不把半截正文、部分工具或超长参数加入模型历史。已经展示的流式片段及失败重试按 Agent 文档处理。

文字聚合规则：

- `delta.content` 缺失或为 null 时无新增文字；出现时必须是 string，其他类型和 content parts 数组非法。
- 非空字符串立即进入待展示队列，同时按顺序加入本次 assistant content；空字符串不展示。
  展示队列保留承载该字符串的 OpenEvent 模型事件 seq，供展示合批及 WAL 来源引用；展示批次不改变正式 assistant 的聚合结果。
- 原样保留所有空格、换行和制表符，不以片段是否“看起来空白”决定丢弃。
- Chat 的占位 start、首批正文替换、后续合批追加、重试 reset、发布顺序和确认边界只由
  [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)及 [WAL 显示来源](AGENT_WAL_PROTOCOL_cn.md#5-副作用意图和显示来源)定义；
  来源关联记录在 WAL 中，本文不定义或依赖 Chat extensions 中的来源字段。

明确拒答规则：

- 只识别 Provider 明确给出的 `refusal` 字段，不靠正文中的“不能”“抱歉”等词判断拒答。
- 流式 `delta.refusal` 缺失或为 null 时无新增拒答；出现时必须是 string，按到达顺序连接。
  聚合后非空才表示明确拒答；空字符串不产生拒答内容。拒答文字不混入 `content`，不在成功终态和完整校验前展示。
- 在成功模型终态和 `finish_reason="stop"` 下，非空 `refusal` 是可接受的回答，不因正文为空而失败，
  不触发失败重试或会话冻结。它不能与 `tool_calls` 同时出现；同时出现按响应格式冲突处理，不执行工具。
- 明确拒答与普通 `content` 同时存在时，回放的 `assistant` 事件分别以 `text` 和 `refusal` 保存原文，Chat 最终只展示拒答原文。
  若本次已经展示了普通正文，用拒答替换；否则用拒答替换占位，只展示一次。
  接受拒答后结束本次回答，不执行工具，后续正常用户输入仍可处理。
- 隐藏 reasoning、`content_filter` 完成原因以及 `llm.v1` 自身的拒绝 result 都不等于这里的模型拒答。

工具聚合规则：

- `delta.tool_calls` 存在时必须是 array；每个元素必须带非负 integer `index`，用于本次调用内汇聚。
- 相同 index 的 `id`、`function.name`、`function.arguments` 字符串分片分别按到达顺序连接。
- `type` 出现时必须为 `function`；缺失字段表示本片没有新增值，不提前用缺失判断最终非法。
- 不解析或执行尚未拼完的 arguments；最终工具按 index 升序排列，index 必须从 0 连续递增。
- 汇聚完成后 ID 必须非空且唯一，name 必须为三项工具之一，arguments 必须可解析为 JSON object。
- 完整 arguments 再按第 5 节严格校验；任意工具非法则整个 assistant 不接受，也不执行其它合法工具。
- Provider 外层额外元数据不进入规范化 assistant；arguments 内未知字段仍然非法。

只有收到当前调用的成功模型终态、完整校验通过并确认仍适用的所需展示后才进行最终接受；取消与结果的先后处理见 [Agent 会话和回答](CHAT_MODEL_AGENT_cn.md#会话和回答)。HTTP 成功、收到若干片段或 finish_reason 出现，
都不能单独代替 `llm.v1` 终态。choice 0 的最终完成原因按以下规则判断：

- 没有工具调用时只接受 `finish_reason="stop"`；存在工具调用时只接受 `finish_reason="tool_calls"`。
- 中间片段允许 finish_reason 缺失或为 null；流结束前必须观察到有效的非空完成原因。
- 重复的相同完成原因不改变结果，互相冲突的完成原因使结果非法；完成原因之后不能再追加正文、拒答或工具参数。
- `length`、`content_filter`、旧式 `function_call`、未知值以及终态时仍缺失完成原因，都不是成功结果。
- 完成原因不替代工具 schema 校验；拒答按本节单独接受，隐藏 reasoning 不当成可见 content。
- 聚合 content 为 null 或空字符串，既没有工具调用，也没有非空 refusal 时，视为没有给出可用结果，按模型失败处理；
  不能静默把本次输入标为已回答。含空白的非空字符串仍按前述规则原样保留。

失败后，只有按 Agent 的重试规则继续且已有可继续写入的 Chat turn 时，才先将回复 reset 回占位，再把新尝试输出
写入同一 turn；占位写入未决、首段正文前失败、重试上限、取消、冻结和新用户输入唤醒的处理不由本文另行定义。
新尝试必须重新初始化 content、refusal、tool_calls、finish_reason 和全部分片聚合状态，不能把旧尝试的任何前缀接入新输出。
事件先按模型 stream 归属筛选，旧尝试迟到的合法片段也不能通过新聚合器进入 Chat 或输入事件。

流式 request 也可能合法地得到普通 JSON：此时 completed `infer.end.body` 包含完整响应，可能没有 append。
Agent 从其中的 choice 0 assistant message 提取 string/null content、string/null refusal 和完整 tool_calls，
其中 refusal 缺失、null 或空字符串表示没有明确拒答，其他非字符串值非法；
并使用该 choice 的 finish_reason 按同一最终规则校验，缺失时同样失败。
不能因为没有 append 判定响应为空；完整 body 按本节相同的 content/refusal 展示选择及 Agent 展示规则
确定最终展示单元，需要时整体替换占位或临时正文，只展示一次，不与流式增量重复连接。
展示来源与提交记录见 WAL 协议，消息大小额度见 Agent 配置。
带 body 的拒绝 result、failed/interrupted end、获胜 cancel 都不是可执行工具的成功模型结果。
