# OpenEvent Agent Demo

[English version](README.md)

OpenEvent Agent Demo 演示如何把 OpenEvent、IM 模块、model-proxy 模块和
OpenEvent View 组合成一套本地 Agent 运行环境。该 demo 把 P2P IM 会话连接到
OpenAI-compatible 模型 provider，所有输入、模型请求、模型结果、命令调用、回复和 Agent WAL
记录都通过 OpenEvent channel 流转。

## 快速开始

1. 构建 Ubuntu 24.04 本地运行环境并激活 venv：

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
source runtime/venv/bin/activate
```

系统依赖包、`--skip-apt`、自定义仓库地址和常见失败处理见
[docs/UBUNTU_ENVIRONMENT_cn.md](docs/UBUNTU_ENVIRONMENT_cn.md)。

2. 在 [openevent-stack/stack.yaml](openevent-stack/stack.yaml) 中填入真实 IM 和模型
   provider 配置。
3. 校验并应用整套 stack：

```bash
./openevent-stack/bootstrap.sh --dry-run
./openevent-stack/bootstrap.sh --apply
./openevent-stack/status.sh
```

## 本地全套运行

`openevent-stack/` 是本地运行 OpenEvent Agent demo 全套组件的模板目录。配置模板和脚本都直接放在这个目录下。

当前组件：

- `openevent` server
- `im-p2p-syncer`
- `model-proxy`
- `cmd-worker`
- `im-model-agent`
- `openevent-view`

本地命令能力依赖 `openevent-modules-cmd`，它提供 `openevent.cmd_sdk` 包和
`cmd-worker` 进程。stack 会安装并启动 `cmd-worker`，创建或复用每个 session 的
`cmd.v1` channel，生成 `cmd-worker` 配置，并让 Agent 通过 OpenEvent 调用本地 shell 命令。

应用 stack 前先按本机环境修改：

- `openevent-stack/stack.yaml`

`scripts/bootstrap_ubuntu_venv.sh` 会把本机路径覆盖配置写到 `openevent-stack/config/env.sh`。
已跟踪的 `openevent-stack/env.sh` 保持为可移植默认模板。

`bootstrap.sh --apply` 会通过 `scripts/reconcile_runtime.py` 生成核心组件的最终配置，启动/重启核心进程，并创建或复用 OpenEvent token/channel。`openevent-view` 是 agent demo 的必须组件，依赖 OpenEvent，只会在核心流程完成后启动。

日常只需要用这几个脚本：

| 脚本 | 用途 |
| --- | --- |
| `openevent-stack/bootstrap.sh` | 初始化/刷新整套运行配置。`--dry-run` 只校验并打印计划；`--apply` 会启动 OpenEvent、创建/复用 token 和 channel、写出最终配置，启动/重启核心进程，最后在 OpenEvent 已运行时启动 view，并打印进程启动结果。 |
| `openevent-stack/start.sh` | 按顺序启动已经生成好配置的进程：OpenEvent、model-proxy、cmd-worker、IM syncer、Agent，最后启动依赖 OpenEvent 的 view，并打印进程启动结果。不会创建 token/channel。 |
| `openevent-stack/stop.sh` | 按反向顺序停止所有本地进程。 |
| `openevent-stack/status.sh` | 显示所有本地进程状态。 |
| `openevent-stack/logs.sh` | 不带参数列出日志文件；带进程名时跟随该进程日志，例如 `./openevent-stack/logs.sh model-proxy`。 |

这些辅助文件通常只在配置或排错时需要关注：

| 文件 | 用途 |
| --- | --- |
| `openevent-stack/env.sh` | 可移植的本机路径默认值；生成的机器特定覆盖配置在 `openevent-stack/config/env.sh`。不放组件运行参数。 |
| `openevent-stack/stack.yaml` | 整套组件的声明式模板：端口、OpenEvent 存储路径、principal、IM provider、模型 provider、session/channel 名称、view 端口。 |
| `openevent-stack/common.sh` | 所有脚本共享的路径、目录和 `PYTHONPATH` 设置。 |
| `openevent-stack/process.sh` | 本地进程管理器，给 `reconcile_runtime.py` 充当简单的 `supervisorctl` 替代，也被 `start/stop/status` 调用；会清理同配置的陈旧重复进程，避免同端口多实例导致 token/channel 状态不一致。 |
| `openevent-stack/render-view-config.sh` | 根据 `stack.yaml` 生成 `config/openevent-view.yaml`。这是 `bootstrap.sh`/`start.sh` 启动 view 前的内部步骤；view 依赖 OpenEvent，不单独启动。 |

生成后的配置、状态、计划和密钥文件统一写到 `openevent-stack/config/`。

其他运行产物默认写到 `openevent-stack/` 下：

- `data/`
- `logs/`
- `run/`
- `workdir/`

通过 `openevent-stack/process.sh` 启动的组件会以 `openevent-stack/workdir/`
作为当前工作目录。Agent 的 `exec` 工具遵循 `cmd.v1` 规则：tool call 未传
`workdir` 时，命令在 `cmd-worker` 当前工作目录执行，因此默认命令工作目录是
`openevent-stack/workdir/`。

## 文档

本仓库主要文档：

| 文档 | 用途 |
| --- | --- |
| [docs/UBUNTU_ENVIRONMENT_cn.md](docs/UBUNTU_ENVIRONMENT_cn.md) | Ubuntu 24.04 环境和 venv 准备 |
| [docs/RUNTIME_RECONCILER_cn.md](docs/RUNTIME_RECONCILER_cn.md) | 声明式运行配置、生成文件、token/channel 协调 |
| [docs/IM_MODEL_AGENT_cn.md](docs/IM_MODEL_AGENT_cn.md) | Agent 配置和持续处理概览 |
| [docs/AGENT_WAL_PROTOCOL_cn.md](docs/AGENT_WAL_PROTOCOL_cn.md) | WAL 字段、稳定 ID、发布对账、retry/blocked 和恢复协议 |

相关项目文档：

| 主题 | 项目文档 |
| --- | --- |
| OpenEvent gRPC API 与 proto | `openevent-sdk` 的 `docs/API.md`、`proto/openevent.proto` |
| OpenEvent 服务端构建/配置 | `openevent` 的 `README.md`、`docs/CONFIG.md` |
| IM payload 协议 | `openevent-modules-im` 的 `docs/IM_PROTOCOL.md` |
| IM SDK 与 P2P syncer | `openevent-modules-im` 的 `docs/IM-PROTOCOL-SDK.md`、`docs/IM-P2P-SYNCER.md` |
| LLM payload 协议 | `openevent-modules-model-proxy` 的 `docs/LLM_PROTOCOL.md` |
| model-proxy worker 与 SDK | `openevent-modules-model-proxy` 的 `docs/CONFIGURATION.md`、`docs/SDK_USAGE.md` |
| 命令协议、SDK 与 worker | `openevent-modules-cmd` 的 `docs/CMD_PROTOCOL.md`、`docs/CONFIGURATION.md`、`docs/SDK_USAGE.md` |
| OpenEvent View | `openevent-view` 的 `README.md` |

## 测试

本仓库测试入口：

```bash
make test
```

测试使用当前 Python 环境中已经安装的依赖包，包括 `openevent-modules-cmd`。如果缺少依赖包，请先安装到当前环境。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `im_model_agent/` | Agent 进程实现 |
| `scripts/reconcile_runtime.py` | 声明式运行配置、channel/token 协调 |
| `scripts/bootstrap_ubuntu_venv.sh` | Ubuntu 24.04 venv 环境准备 |
| `openevent-stack/` | 本地全套组件配置模板和进程脚本 |
| `docs/` | Agent 专属协议、运行和环境文档 |
| `tests/` | 单元测试和 stack 脚本检查 |
