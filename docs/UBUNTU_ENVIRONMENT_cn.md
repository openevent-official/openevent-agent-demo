# Ubuntu 环境准备

本项目的环境脚本准备 Chat Agent 所需的 OpenEvent、Chat、Model Proxy、Cmd、View 和本项目安装包。

## 准备与安装

在项目目录运行：

```bash
scripts/bootstrap_ubuntu_venv.sh
# 系统依赖已安装时：
scripts/bootstrap_ubuntu_venv.sh --skip-apt
# 使用另一个环境安装工作区：
scripts/bootstrap_ubuntu_venv.sh --workdir /opt/openevent/runtime --skip-apt
```

默认工作区为本项目下 `runtime/`。脚本安装编译工具、CMake、Protobuf/gRPC、RocksDB、yaml-cpp
以及 Python 开发/venv 支持，创建 `venv/`，每次运行都在 `sources/` 中拉取各依赖远端默认分支的最新提交。
SDK 在 `sources/openevent/openevent-sdk` 中单独拉取；服务端构建和 SDK 安装使用这一份 checkout。
已有 checkout 不得有源码文件改动；脚本不会覆盖已有源码修改。
抓取失败后可在同一工作目录重跑；已完成的干净克隆会被复用。
脚本推进 SDK 提交后，OpenEvent checkout 中会显示子模块引用变化；仅此变化不会阻止再次运行。
脚本在日志中记录各组件实际选中的提交。

OpenEvent 使用 `make build BUILD_TYPE=Release`。Python 包通过各自的 `make install` 构建并安装：
先安装上述 SDK checkout，再安装 Chat、Model Proxy、Cmd、View，最后安装本项目。
安装只使用本次成功构建的产物；各组件包即使版本号相同也会替换，第三方依赖只补齐缺失或不满足包声明的项。
本项目使用脚本所在工作区的源码构建，包括未提交的修改；脚本不会自动拉取或替换这个工作区。
环境脚本为整套组件在部署 venv 中准备构建工具。单独构建或安装 Demo 的要求见[构建与安装](../README_cn.md#构建与安装)。
临时构建产物进入相应项目 `build/`，发布产物进入 `dist/`。
环境脚本自身的 Python 操作（包括创建 venv、安装构建工具）使用本项目 `build/pip-cache/` 和 `build/tmp/`
存放缓存与临时文件，即使指定其他 `--workdir` 也一样；各组件的构建与安装仍由其 Makefile 管理这些目录。
安装失败立即停止，不使用旧产物继续报告成功。

脚本的 URL/提交覆盖变量为：
`OPENEVENT_URL/REF`、`OPENEVENT_SDK_URL/REF`、`OPENEVENT_MODULES_CHAT_URL/REF`、
`OPENEVENT_MODEL_PROXY_URL/REF`、`OPENEVENT_MODULES_CMD_URL/REF`、`OPENEVENT_VIEW_URL/REF`。
全部 `*_REF` 默认是 `HEAD`，表示每次运行时远端默认分支的最新提交。
也可显式指定分支、tag 或提交；选用的版本必须继续满足 [Runtime 依赖要求](RUNTIME_RECONCILER_cn.md#依赖)。
`PYTHON_BIN` 选择创建 venv 的 Python。此脚本从源码构建 SDK，需使用 Python 3.10–3.12，并在 venv 中准备 pip 22.3 或更高版本。`JOBS` 指定构建并发。

安装后验证模块导入、SDK、Chat 按已知状态构造 `TurnWriter` 与 `serialize_payload`，以及 reset、流式和指定 UUID 能力；
不通过 PYTHONPATH 使用 SDK checkout。
脚本生成 `openevent-stack/runtime/config/env.sh`，写入部署 Python 和 OpenEvent server 路径。
Stack 脚本依次采用显式环境变量、这些生成路径、`env.sh` 中的默认值；因此自定义 `--workdir` 会生效，
同时保留显式 `PYTHON_BIN`、`OPENEVENT_SERVER_BIN` 的优先级。
不启动服务，不填写模型凭据或网页 token。

## 配置与启动

按 [Runtime 配置](RUNTIME_RECONCILER_cn.md) 编辑唯一的部署配置 `openevent-stack/stack.yaml`，
准备用户、Agent、Model Proxy、Cmd Worker 四个不同身份、Chat web token、模型 Provider 和服务地址。
Runtime 根据该配置启动 OpenEvent，准备 token 和 Model、Cmd、WAL Channel，
保存绑定并生成全部组件配置。采用已有 Channel 时可显式填写对应 ID，具体规则见 Runtime 配置。

### 启动完整部署

```bash
openevent-stack/bootstrap.sh --dry-run
openevent-stack/bootstrap.sh --apply
openevent-stack/status.sh
openevent-stack/logs.sh chat-model-agent
openevent-stack/stop.sh
openevent-stack/start.sh
```

启动顺序、对象命令环境和停止步骤以 Runtime 文档为准。
本地 stack 脚本固定使用 `openevent-stack/runtime/` 存放运行配置、数据和日志，不受 `runtime.root` 改动影响；
这是本地脚本的约定，通用命令入口的运行目录配置见 Runtime 文档。
它与环境安装脚本 `--workdir` 指定的工作区不同，后者默认为项目根目录下的 `runtime/`，存放编译源码及 Python venv。
环境准备脚本不自动执行浏览器、模型或命令任务。

## 更新已运行的部署

更新代码且部署协议不变时，先停止服务，再安装代码，最后启动服务。
仅安装包不会让已经运行的进程换用新代码；配置不变时，`apply` 和 `start` 也不会强制重启这些进程。

使用当前工作区更新本项目时，在项目目录按顺序执行，每一步成功后再继续；Python 必须使用当前部署的环境：

```bash
openevent-stack/stop.sh
make install PYTHON=/absolute/path/to/deployment/python
openevent-stack/start.sh
```

如果要先拉取本项目远端的最新代码，在干净工作区中执行 `git pull --ff-only`，再执行 `make install`。
工作区有本地修改时，先保存或处理这些修改再拉取；安装本身会直接使用这些修改。

更新依赖时，将中间的安装步骤换成相应项目的 `make install`，或使用原来的工作区路径重新运行环境准备脚本。
环境脚本默认拉取依赖远端默认分支的最新提交；需要其他来源或版本时再显式设置 URL/REF 参数。
配置也发生变化时，最后执行 `openevent-stack/bootstrap.sh --apply`，而不是 `start.sh`。
停止确认与重启前提见 [Runtime 文档](RUNTIME_RECONCILER_cn.md#启动更新与停止)。
