# OpenEvent Agent Demo

[English version](README.md)

OpenEvent Agent Demo 是一个面向开发者的可运行参考应用，演示如何基于 OpenEvent 构建聊天 Agent。
它用事件把网页对话、模型调用和 Linux 命令执行连起来。你可以直接部署整套应用，也可以参考它的模块分工开发自己的 Agent。

## 架构与模块分工

每个会话在 OpenEvent 中有自己的 Chat Channel。同一套部署的所有会话共用另外三条 Channel。Model Proxy 和 Cmd Worker
分别通过 Model、Cmd Channel 接收请求并返回结果。Agent 把自己的决策和源事件引用写入 WAL（预写日志）Channel。

### OpenEvent Server

服务端按全局顺序保存各条 Channel 的消息，并控制谁可以读写。附件内容单独保存为不可变对象。
Agent 重新上线后可以按顺序读取保留下来的事件，包括离线期间收到的用户消息。[OpenEvent 项目](https://github.com/openevent-official/openevent)

### Chat

Chat 提供浏览器页面、HTTP 服务和 Chat SDK。用户新建会话时，Chat Server 创建对应的 Channel，把会话显示名称保存在本地会话目录。
它把用户消息和附件引用发布到这条 Channel，再从中读取 Agent 的回复，陆续展示在浏览器中。[Chat 项目](https://github.com/openevent-official/openevent-modules-chat)

### Chat Agent

一个 Agent 进程会发现自己已加入的 Chat Channel，为每个会话分别维护模型历史和工具队列。它组织模型输入、向 Chat 流式写回回答，
并把工具结果放进后续模型调用。重启后，Agent 根据 WAL 中的决策以及所引用的 Chat、Model 和 Cmd 事件恢复各会话的处理进度。[Chat Agent 指南](docs/CHAT_MODEL_AGENT_cn.md)

### Model Proxy

生成回答或压缩历史时，Agent 准备好本次调用的会话上下文。Model Proxy 持有模型服务的凭据，调用配置的 OpenAI 兼容服务，
再把响应片段和最终状态写回 Model Channel。[Model Proxy 项目](https://github.com/openevent-official/openevent-modules-model-proxy)

### Cmd

Cmd Worker 在自己的 Linux 主机上执行 Agent 请求的 Shell 命令，默认使用与 Agent 共享的会话工作目录。它把执行结果写回 Cmd Channel，
将标准输出和错误输出保存在本地文件中，Agent 可以按需读取其中一段。[Cmd 项目](https://github.com/openevent-official/openevent-modules-cmd)

### OpenEvent View

View 提供独立的只读网页和 API，用来查看 Channel 信息和消息历史。排查问题时，可以使用自己的 OpenEvent 凭据，
在有权读取的 Channel 中追踪一次模型请求或命令执行结果。[OpenEvent View 项目](https://github.com/openevent-official/openevent-view)

## Agent 运行行为

在浏览器中新建会话，就可以提问或附上文件。Agent 的回答会逐步显示。
命令运行期间，你仍可以在同一会话里补充消息，也可以单独取消某条尚未结束的回答。[会话和回答](docs/CHAT_MODEL_AGENT_cn.md#会话和回答)

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

## 构建与安装

在项目根目录运行。Make 默认使用当前终端的 `python3`，也可用 `PYTHON` 指定装有 pip 的 Python 3.10 或更高版本。

| 命令 | 用途 |
| --- | --- |
| `make build` | 由 pip 在隔离环境中准备构建工具，成功后把本次生成的 wheel 放入 `dist/`。构建阶段只需要[包声明](pyproject.toml)中的构建依赖。 |
| `make install` | 重新构建，将本次 wheel 装入所选 Python 环境。pip 只补齐缺失或版本不符的运行依赖，即使版本号未变也会替换 Demo 本身。需要 pip 22.3 或更高版本。 |
| `make clean` | 清理本项目的构建文件、缓存和发布包，保留 Python 环境、离线依赖和其他项目的产物。 |

以下命令将 Demo 安装到部署用的虚拟环境。

```bash
make install PYTHON=/opt/openevent/venv/bin/python
```

构建中间文件和缓存存放在 `build/`。构建和安装会清除外部 `PYTHONPATH`、`PYTHONHOME`。如果所选 Python 能导入用户目录中的包，也支持
`PIP_USER=1 make install`。安装前请取消 pip 的 `--python`、`--root` 设置。要用 `--target` 或 `--prefix`
安装到指定目录，先运行 `make build`，再把本次 wheel 的确切路径交给 `python3 -m pip install`。

在 `pip.conf` 中，用 `[global]` 设置共用包源。需要分开时，用 `[wheel]` 指定构建依赖的包源，用 `[install]` 指定运行依赖的包源。
依赖解析或下载失败时，安装会在替换已有 Demo 之前停止。离线安装时，先把适合目标 Python
和平台的构建依赖、运行依赖及其间接依赖放入 `build/wheelhouse/`，再执行以下命令。

```bash
PIP_NO_INDEX=1 PIP_FIND_LINKS=build/wheelhouse make install
```

安装后提供两个命令：

```bash
chat-model-agent --config /etc/openevent-agent/agent.json
openevent-read-object 12345
```

后者在 Cmd 任务的运行环境中取得随执行请求传递的附件凭据，由 Runtime 提供对象服务地址，执行时不依赖 Agent 在线；具体用法见
[Agent 运行文档](docs/CHAT_MODEL_AGENT_cn.md)。

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
