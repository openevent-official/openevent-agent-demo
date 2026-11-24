# 模型请求示例

[English version](README.md)

这些文件是模型 I/O v4 的完整 `POST /v1/chat/completions` 请求 body，与当前实现格式一致；示例尚未连接真实外部 Provider 执行。
字段、工具和压缩格式以[公开模型 I/O 契约](../MODEL_IO_cn.md)为准。

## 连续两次普通输入

1. [第一次输入](context-input-1.json)：用户要求检查 `app.py` 是否可读，之后与所附的 `app-reference.py` 对照；另一份附件 `old-notes.txt` 已经过时。Agent 已接受检查调用，工具结果尚未提供时，用户接着问这条检查是什么意思。这条新消息没有附件，不复制前一条的附件信息。
2. [第二次输入](context-input-2.json)：保留第一次的事件，随后追加模型给出的解释和工具真实结果，通过 `task_id` 对应原调用。

这是会话中连续两次普通模型调用，不是会话从零开始的头两次调用。第一次请求之前已经有一次模型输出发起了检查任务。
两次之间没有触发历史压缩，因此 `summary` 都为 null。第二次提供完整的当前上下文，不是只发送新增事件；工具结果仍排在新用户消息和解释之后。

## 压缩与采用摘要

3. [历史压缩请求](context-compaction.json)：将前述用户请求 301 与工具调用 T 作为完整旧前缀，直接放进 `events` 数组，此前没有摘要，因此 `summary` 为 null。`max_summary_bytes` 明确给出这次摘要的大小上限，压缩请求不传 `tools`。
4. [采用摘要的普通请求](context-with-summary.json)：演示如果该前缀已取得有效摘要，第二次普通输入如何改为“摘要＋其后事件”。摘要正文保留后续对照需要的 `app-reference.py` 和原 `object_id=901`，丢弃过时的 `old-notes.txt`；没有另附完整附件目录。T 的原任务编号、所属回答和检查目的保留在摘要正文中，后面的结果通过 `task_id` 与它对应；程序不再附加完整命令参数表。

后两份示例是同一段历史的另一种呈现，用于说明压缩格式，不是宣称这段很短的历史已经需要压缩，也不是 Provider 实测结果。
示例摘要是人工写出的合法示意正文，不要求模型逐字生成。它没有覆盖用户 703、解释回复或工具结果，这些事件仍完整留在 `events`。
每批压缩材料都由完整、连续的事件组成，程序按请求预算选择一批，不切开 JSON、字段或正文。模型决定摘要正文应保留哪些附件信息，程序不从全历史重新补齐文件清单。这里保留和丢弃各一个附件，示范这种取舍；摘要文字不能自行创建附件来源或读取凭据。

本组示例采用 `context_max_input_bytes=1048576`，假定默认发送容量足够，有效运行上限等于配置上限。
按当前默认提示词、工具定义及[预算规则](../CHAT_MODEL_AGENT_cn.md#上下文字节预算)，
完整请求的固定基线为 7461 字节，推导出 `max_summary_bytes=520557`。历史整体与单条输入各有 520557 字节额度；
两者都按最终请求的编码增量计量，事件额外预留一个数组逗号。历史目标包含摘要正文和仍未压缩的旧事件，不能只检查摘要正文。
`7461 + 520557 + 520557 = 1048575 <= 1048576`，因此达标的历史可以再接收一条达到准入上限的完整输入。
这是随配置计算的结果，不是独立的摘要配置项；提示词或工具定义变化后须重新计算。每批摘要满足自己的上限后仍可能需要继续压缩，直到整个旧历史达标。

## 默认提示词与使用

仓库提供两份完整 UTF-8 默认文件：

- [普通系统提示词](../../openevent-stack/prompts/system.txt)
- [上下文压缩提示词](../../openevent-stack/prompts/context_compression.txt)

普通请求示例的 system 正文与第一份文件完全一致，压缩请求与第二份一致，包括末尾换行。没有隐藏追加提示词或模板替换。
部署规格仍须显式填写 `agent.system_prompt_file` 和 `agent.context_compression_prompt_file`；以 `openevent-stack/stack.yaml` 为入口时，可分别使用
`./prompts/system.txt` 和 `./prompts/context_compression.txt`。加载和更新规则见[Runtime 提示词文件](../RUNTIME_RECONCILER_cn.md#提示词文件)。
Runtime 已实现这些文件入口的加载与配置生成，具体使用方式见 [Runtime 提示词文件](../RUNTIME_RECONCILER_cn.md#提示词文件)。

外层请求遵循 [OpenAI Chat Completions 请求格式](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
`example-model` 必须替换为部署模型名，不表示存在这个模型。普通请求包含同一套三项工具定义，压缩请求不提供工具。
文件为便于阅读而缩进，预算计算使用[公开计量规则](../CHAT_MODEL_AGENT_cn.md#上下文字节预算)。

`messages[1].content` 是 JSON **字符串**：普通请求解码后只有 `summary` 和 `events`，压缩请求另有正整数 `max_summary_bytes`。
两种请求的 `summary` 都是摘要正文字符串或 null；这些不是外层 API 字段。
`role=user` 是上下文容器，只有内部 `type=user` 事件表示用户发言。`stream=true` 表示模型输出流式返回，与用户消息无关。
工具定义用于输出新的原生 `tool_calls`；历史调用和真实结果通过事件回放，不添加工具接收回执。
