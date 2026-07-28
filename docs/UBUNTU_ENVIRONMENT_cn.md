# Ubuntu 环境准备

[English version](UBUNTU_ENVIRONMENT.md)

本文说明如何从一台 Ubuntu 空环境构建 OpenEvent Agent Demo 的本地运行环境。
当前只覆盖 Ubuntu。

## 目标

完成后本地会得到：

- `runtime/src/`：从 GitHub 拉取并固定到明确 commit 的 OpenEvent、IM、model-proxy、cmd 和 view 源码。
- `runtime/venv/`：Agent Demo 使用的 Python venv。
- `runtime/src/openevent/build/openevent_server`：本地构建出的 OpenEvent server。
- `openevent-stack/config/env.sh`：指向上述 venv 和 server 二进制的本机路径覆盖配置。

脚本不会启动整套 stack，也不会写入真实 IM/LLM 凭据。

## 前置要求

建议使用 Ubuntu 24.04 LTS。该版本已经包含 Git 和 Python 3.12 运行时，仍需补齐以下
构建/运行依赖：

- Python 3.10+
- CMake 3.20+
- C++20 编译工具链
- Protobuf、`protoc`、`grpc_cpp_plugin`
- RocksDB 开发库
- yaml-cpp 开发库

`scripts/bootstrap_ubuntu_venv.sh` 默认会通过 `apt-get` 安装这些系统依赖。如果机器已经
准备好系统依赖，可以使用 `--skip-apt`。

如果要先手动补齐缺失依赖包，执行：

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential \
  cmake \
  pkg-config \
  protobuf-compiler \
  protobuf-compiler-grpc \
  libprotobuf-dev \
  libgrpc++-dev \
  librocksdb-dev \
  libyaml-cpp-dev \
  python3-dev \
  python3-venv \
  python3-pip
```

## 从仓库根目录开始

本文假设你已经在 `openevent-agent-demo` 仓库根目录。脚本会从 GitHub 公开仓库拉取其他
OpenEvent 依赖项目。

构建完整本地运行环境。默认让脚本安装缺失的系统依赖：

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

如果已经手动安装好系统依赖，可以跳过脚本里的 apt 步骤：

```bash
scripts/bootstrap_ubuntu_venv.sh --workdir runtime --skip-apt
```

激活 venv：

```bash
source runtime/venv/bin/activate
```

## 脚本做了什么

`scripts/bootstrap_ubuntu_venv.sh --workdir runtime` 会按顺序执行：

1. 安装 Ubuntu 系统依赖。
2. 从 GitHub 公开仓库拉取依赖项目，并 checkout 脚本声明的固定 commit。
3. 初始化 OpenEvent server 的 Git submodule；Python SDK 使用其中固定的 `openevent-sdk`。
4. 创建 `runtime/venv`。
5. 构建 OpenEvent server。
6. 通过各子项目的 `make install` 把 OpenEvent 自带 SDK、IM、model-proxy、cmd 和 view 安装到同一个 venv。
7. 生成 `openevent-stack/config/env.sh`。
8. 验证 venv 中可以 import 运行所需模块。

bootstrap 依赖公开仓库：

```bash
https://github.com/openevent-official/openevent.git
https://github.com/openevent-official/openevent-modules-im.git
https://github.com/openevent-official/openevent-modules-model-proxy.git
https://github.com/openevent-official/openevent-modules-cmd.git
https://github.com/openevent-official/openevent-view.git
```

脚本中的 `*_REF` 默认值是经过组合验证的 commit。可以同时覆盖仓库地址和对应 commit/tag：

```bash
OPENEVENT_URL=https://github.com/openevent-official/openevent.git \
OPENEVENT_REF=a1d2d97d0870dd5fc61af329bf0cb94cd124aafa \
OPENEVENT_MODULES_IM_URL=https://github.com/openevent-official/openevent-modules-im.git \
OPENEVENT_MODULES_IM_REF=8aebcb35506d7e60384b548800830273bc1781f7 \
OPENEVENT_MODEL_PROXY_URL=https://github.com/openevent-official/openevent-modules-model-proxy.git \
OPENEVENT_MODEL_PROXY_REF=60fe36fe30fa5b92b1dffa535082686ea28e75ef \
OPENEVENT_MODULES_CMD_URL=https://github.com/openevent-official/openevent-modules-cmd.git \
OPENEVENT_MODULES_CMD_REF=bc92ef9c4b46295e098021d5a637c8c7b09dc9b0 \
OPENEVENT_VIEW_URL=https://github.com/openevent-official/openevent-view.git \
OPENEVENT_VIEW_REF=9127056bf0a88f7091c3d1b21e4186ea60180706 \
scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

覆盖 `*_REF` 后，这些 ref 必须仍是一组协议和配置契约兼容的版本。脚本只保证默认组合经过验证。

## 常见失败

缺少 Python 3.10+：

```text
Python 3.10+ is required.
```

处理方式：安装 Python 3.10 或更新版本，并用 `PYTHON_BIN` 指定：

```bash
PYTHON_BIN=/usr/bin/python3.10 scripts/bootstrap_ubuntu_venv.sh --workdir runtime
```

缺少 CMake 3.20+：

```text
CMake 3.20+ is required
```

处理方式：升级 CMake 后重新运行脚本。

GitHub 公开仓库拉取失败：

```text
failed to clone ...
```

处理方式：检查网络、代理和仓库地址；如果需要使用镜像地址，用对应环境变量覆盖仓库地址。

已有 checkout 存在本地修改：

```text
existing checkout has local changes
```

处理方式：进入对应 `runtime/src/<project>`，提交、stash 或删除该目录后重跑脚本。
