# Ubuntu 环境准备

本项目的环境脚本准备 Chat Agent 所需的 OpenEvent、Chat、Model Proxy、Cmd、View 和本项目安装包。
不安装 IM 包，不需要飞书/Lark 凭据，也不转换旧 IM 数据。

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
环境脚本按项目要求补齐构建工具的最低版本；已有版本满足要求时继续使用。
临时构建产物进入相应项目 `build/`，发布产物进入 `dist/`。
环境脚本自身的 Python 操作（包括创建 venv、安装构建工具）使用本项目 `build/pip-cache/` 和 `build/tmp/`
存放缓存与临时文件，即使指定其他 `--workdir` 也一样；各组件的构建与安装仍由其 Makefile 管理这些目录。
安装失败立即停止，不使用旧产物继续报告成功。

脚本的 URL/提交覆盖变量为：
`OPENEVENT_URL/REF`、`OPENEVENT_SDK_URL/REF`、`OPENEVENT_MODULES_CHAT_URL/REF`、
`OPENEVENT_MODEL_PROXY_URL/REF`、`OPENEVENT_MODULES_CMD_URL/REF`、`OPENEVENT_VIEW_URL/REF`。
全部 `*_REF` 默认是 `HEAD`，表示每次运行时远端默认分支的最新提交。
也可显式指定分支、tag 或提交；选用的版本必须继续满足 [Runtime 依赖要求](RUNTIME_RECONCILER_cn.md#依赖与验证)。
`PYTHON_BIN` 选择创建 venv 的 Python，`JOBS` 指定构建并发。

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
已有 IM spec/state 会明确报错；使用新的 Chat 部署状态和 WAL。

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

更新本项目或依赖时，先停止部署，再安装新版本，最后启动。
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
正常升级保留已有部署数据；停止确认与重启前提沿用 [Runtime 文档](RUNTIME_RECONCILER_cn.md#启动更新与停止)。

## 开发验证

跨模块协议或 SDK 改动使用独立测试环境，显式提供当前要验证的五个公开仓库源码目录：

```bash
make test-current-dependencies PYTHON=python3 \
  SDK_SOURCE=/path/to/openevent-sdk \
  CHAT_SOURCE=/path/to/openevent-modules-chat \
  MODEL_PROXY_SOURCE=/path/to/openevent-modules-model-proxy \
  CMD_SOURCE=/path/to/openevent-modules-cmd \
  VIEW_SOURCE=/path/to/openevent-view
```

这些路径由调用者选择，没有仓库外部目录默认值。入口在本项目 `build/dependency-test-env/` 创建或复用独立 venv，
依次调用各源码目录的 `make install`，最后构建安装本项目；随后运行 `make test`。
每次均使用本次成功构建的代码，包括源码目录内未提交的修改；即使版本号未变也替换对应项目包，
第三方依赖继续按包声明补齐，不强制重装或整体升级。任何构建、安装或测试失败都立即停止。
此流程不更新源码、不启动服务，也不修改部署 venv。

若只需准备环境，将目标换成 `prepare-test-env`，保留上述参数。随后可反复运行指定用例：

```bash
make test PYTHON=build/dependency-test-env/bin/python TEST_ARGS='-p test_chat_agent_*.py'
```

准备时会核对各模块确实来自这个环境中的已安装发行包；完整入口会清除继承的 `PYTHONPATH` 和 `PYTHONHOME`，
不通过源码路径替代已构建依赖。每个组件自己的构建缓存与中间产物仍由该组件的 `make` 放在其 `build/` 下。

已经自行安装当前依赖时，也可使用选定 Python 直接运行：

```bash
make test PYTHON=/absolute/path/to/python
make install PYTHON=/absolute/path/to/python
```

测试的临时目录和日志位于 `build/`。更换依赖后应重新准备独立环境或执行相应 `make install`，
不能因发行版本号相同而复用旧安装代码；已运行的部署按上节步骤更新。
