# OpenEvent Agent Demo

> 实现状态：事件输入、异步工具、上下文压缩、过长消息拒绝、提示词文件配置及 WAL v5 已实现。已完成的检查与未验收的范围见[验证范围](#验证范围)。

[English version](README.md)

本项目把 OpenEvent、Chat、Model Proxy、Cmd 和 OpenEvent View 组合成可运行的 Chat Agent。
用户在 Chat 中提问、附加文件或取消回答；Agent 流式回复，按需执行命令，并通过 OpenEvent 保存恢复所需的事实。
会话从 Chat 动态发现，不需要逐个配置会话或重新应用部署。
用户消息使用完整的 `turn.single`，Agent 回答仍流式展示。工具运行期间的新用户消息可以触发模型，
同一会话的模型调用和实际工具执行分别串行；旧事件通过摘要压缩进入后续输入。
模型摘要保留必要任务信息，完整工具参数留在执行记录中；历史达到大小目标后，保证能加入一条合规输入。
后续工具结果仍按顺序进入事件历史。
具体行为与额度见 [Agent 文档](docs/CHAT_MODEL_AGENT_cn.md#会话和回答)。

会话发现由全量 Fetch 中的系统消息驱动。
接管条件、Chat 的加入顺序前提和重启行为见 [Agent 文档](docs/CHAT_MODEL_AGENT_cn.md#会话发现)。
WAL 字段见 [WAL 协议](docs/AGENT_WAL_PROTOCOL_cn.md)。文档维护当前方案，不为旧代码保留兼容或迁移设计。
正文展示只合并完整模型片段、不拆分，并用 seq 范围记录来源；
具体契约见 [WAL 显示来源](docs/AGENT_WAL_PROTOCOL_cn.md#5-副作用意图和显示来源)。
当前 WAL 为 `agent.wal.v5`，模型 I/O 版本为 `3`；不兼容旧格式，也不提供迁移流程。
公开格式说明、默认提示词文件、请求示例、实现与部署模板使用同一套配置和协议。

当前 Agent 入口是 `chat-model-agent`，配置版本为 `chat-agent.v1`。
部署使用已经安装的 `openevent-sdk >= 0.13.0` 和 Chat、Model Proxy、Cmd、View 发行包。

## 快速开始

在 Ubuntu 24.04 上准备环境并激活 venv：

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
source runtime/venv/bin/activate
```

脚本默认拉取依赖的最新代码，并安装本项目当前工作区；版本选择和更新方式见 [环境准备](docs/UBUNTU_ENVIRONMENT_cn.md)。在
[openevent-stack/stack.yaml](openevent-stack/stack.yaml) 中填写模型 Provider、用户和 Agent 身份、Chat web token 及服务端口。
两个完整提示词文件的路径与上下文字节预算也在该配置中指定，见 [Runtime 提示词文件](docs/RUNTIME_RECONCILER_cn.md#提示词文件)。
默认正文位于 [system.txt](openevent-stack/prompts/system.txt) 和 [context_compression.txt](openevent-stack/prompts/context_compression.txt)；
编写或修改提示词前阅读[模型输入输出格式](docs/MODEL_IO_cn.md)。
Runtime 自动准备 token 和三个辅助 Channel、生成组件配置；已有 Channel 可按 [Runtime 配置](docs/RUNTIME_RECONCILER_cn.md) 显式指定。然后运行：

```bash
./openevent-stack/bootstrap.sh --dry-run
./openevent-stack/bootstrap.sh --apply
./openevent-stack/status.sh
```

打开配置中的 Chat 页面创建会话。附件通过对象 ID 交给模型；需要正文时，模型在 `exec.object_ids`
中选择本次需要的附件，并调用 `openevent-read-object <object_id>`，具体用法见 [Agent 文档](docs/CHAT_MODEL_AGENT_cn.md)。

停止整套服务：

```bash
./openevent-stack/stop.sh
```

## 构建、安装和测试

在已安装 SDK 和 Chat、Model Proxy、Cmd、View 发行包的 Python 环境中运行：

```bash
make build PYTHON=python3
make install PYTHON=python3
make test PYTHON=python3
```

构建还需要 `setuptools >= 68` 和 `wheel`。`make build` 在 `build/` 中构建，当前 wheel 写入 `dist/`。
`make install` 先重新构建，再按 wheel 声明检查依赖，只补齐缺失或不满足要求的项。
成功后仅强制替换本项目包，即使版本号未变也生效；依赖解析或下载失败时，会在替换已安装的本项目包之前停止。
生成文件、缓存和测试数据放在 `build/`，不会在源码目录生成 Python 字节码。

`make test-install` 在 `build/` 下的临时虚拟环境中离线验证安装顺序和同版本替换；
需要 Python 的 `venv` 支持，无需安装 OpenEvent 组件。

跨模块改动使用 `make test-current-dependencies`：显式选择五个依赖源码目录，在独立环境中重新构建安装后运行测试。
参数及单独准备环境的入口见[开发验证](docs/UBUNTU_ENVIRONMENT_cn.md#开发验证)。不能仅凭版本号相同就沿用旧安装包来证明当前源码兼容。

仅验证 WAL 索引和 Runtime 本地状态写入时，可运行 `make test-unit PYTHON=python3`。
此入口使用同一依赖环境，只运行本地用例子集；它不替代 `make test` 的完整依赖和集成检查。

安装后提供两个命令：

```bash
chat-model-agent --config /etc/openevent-agent/agent.json
openevent-read-object 12345
```

后者在 Cmd 任务的运行环境中取得随执行请求传递的附件凭据，由 Runtime 提供对象服务地址，执行时不依赖 Agent 在线；具体用法见
[Agent 运行文档](docs/CHAT_MODEL_AGENT_cn.md)。

## 验证范围

自动化验证包括单元测试、Runtime 与进程脚本测试，以及使用真实 Chat、Model、Cmd SDK 和内存事件服务的 Agent 集成测试。
覆盖工具运行时处理新输入、旧回复取消隔离、压缩检查点与恢复、多批历史目标、超长消息独立拒绝、失败暂停与新输入唤醒，
以及恢复沿用冻结预算等场景。这些测试通过项目的 `make` 入口运行，不调用真实外部模型 Provider。

2026-11-13 已用实际 Agent 提示词、工具 schema 和历史输入组成的请求测试真实外部模型 Provider，收到 HTTP 200 和完整流式响应。
这项检查验证了该请求能被 Provider 接受并正常返回；它未经过 Agent 与 OpenEvent 的完整事件处理链路，也未执行模型工具。
生产环境中 Chat、Agent、OpenEvent、Model Proxy 和 Cmd 的完整端到端验证尚未完成。

## 日常运行

`openevent-stack/` 提供本地整套组件模板和脚本。使用 `bootstrap.sh` 应用配置，
`start.sh` / `stop.sh` 启停，`status.sh` 查看状态，`logs.sh <进程名>` 跟随日志。
部署配置、启动顺序、共享辅助 Channel 绑定和重启要求统一见
[Runtime Reconciler](docs/RUNTIME_RECONCILER_cn.md)，这里不另行维护一套运行规则。

## 文档

| 文档 | 用途 |
| --- | --- |
| [环境准备](docs/UBUNTU_ENVIRONMENT_cn.md) | Ubuntu 环境、依赖安装和排错 |
| [Runtime Reconciler](docs/RUNTIME_RECONCILER_cn.md) | 整套部署配置、生成文件、token/channel 和进程协调 |
| [Chat Agent](docs/CHAT_MODEL_AGENT_cn.md) | Agent 配置、工具、取消和失败后的使用行为 |
| [模型输入输出格式](docs/MODEL_IO_cn.md) | 事件、摘要、压缩请求、工具和响应格式，默认提示词与完整示例 |
| [Agent WAL 协议](docs/AGENT_WAL_PROTOCOL_cn.md) | 持久字段、稳定键和源事件引用 |

Chat、LLM 和命令的公开协议由各自模块维护；Agent 使用这些公开接口，不重新定义底层协议。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `chat_model_agent/` | Chat Agent 和对象读取命令 |
| `runtime_reconciler/` | 声明式运行配置、channel/token 和进程协调 |
| `scripts/` | 环境准备脚本 |
| `openevent-stack/` | 本地部署模板和进程脚本 |
| `docs/` | 本项目运行和协议文档 |
| `tests/` | Agent、Runtime 和脚本测试 |
