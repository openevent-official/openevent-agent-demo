PYTHON ?= python3
.DEFAULT_GOAL := test
TEST_ARGS ?=
BENCHMARK_ARGS ?=
WHEEL := dist/openevent_agent_demo-0.2.0-py3-none-any.whl
DEPENDENCY_TEST_PYTHON := $(CURDIR)/build/dependency-test-env/bin/python
export PYTHONDONTWRITEBYTECODE := 1
export PIP_CACHE_DIR := $(CURDIR)/build/pip-cache
export TMPDIR := $(CURDIR)/build/tmp

.PHONY: test test-unit test-install prepare-test-env test-current-dependencies benchmark-stack-status check-scripts build install

prepare-test-env:
	mkdir -p build/tmp
	"$(PYTHON)" -I -B scripts/prepare_test_env.py \
		--sdk "$(SDK_SOURCE)" --chat "$(CHAT_SOURCE)" --model-proxy "$(MODEL_PROXY_SOURCE)" \
		--cmd "$(CMD_SOURCE)" --view "$(VIEW_SOURCE)"

test-current-dependencies: prepare-test-env
	$(MAKE) test PYTHON="$(DEPENDENCY_TEST_PYTHON)" PYTHONPATH= PYTHONHOME=

test: check-scripts
	mkdir -p build/tmp build/tests
	PYTHON="$(PYTHON)" ./test.sh $(TEST_ARGS)

test-unit: check-scripts
	mkdir -p build/tmp build/tests
	PYTHONPATH=".:tests$${PYTHONPATH:+:$$PYTHONPATH}" "$(PYTHON)" -m unittest test_chat_agent_wal test_runtime_state $(TEST_ARGS)

test-install:
	mkdir -p build/tmp build/tests
	"$(PYTHON)" -m unittest discover -s tests -p test_install.py $(TEST_ARGS)

benchmark-stack-status:
	mkdir -p build/tmp build/benchmarks
	"$(PYTHON)" -I -B scripts/benchmark_stack_status.py $(BENCHMARK_ARGS)

build:
	rm -rf build/package
	mkdir -p build/package build/tmp dist
	rm -f dist/openevent_agent_demo-*.whl dist/openevent_agent_demo-*.tar.gz
	cp pyproject.toml README.md LICENSE build/package/
	cp -R chat_model_agent runtime_reconciler build/package/
	"$(PYTHON)" -m pip wheel --no-deps --no-build-isolation --wheel-dir "$(CURDIR)/dist" "$(CURDIR)/build/package"
	test -f "$(WHEEL)"

install: build
	"$(PYTHON)" -m pip install "$(WHEEL)"
	"$(PYTHON)" -m pip install --force-reinstall --no-deps "$(WHEEL)"

check-scripts:
	bash -n scripts/bootstrap_ubuntu_venv.sh
	bash -n openevent-stack/bootstrap.sh
	bash -n openevent-stack/common.sh
	bash -n openevent-stack/env.sh
	bash -n openevent-stack/logs.sh
	bash -n openevent-stack/process.sh
	bash -n openevent-stack/start.sh
	bash -n openevent-stack/status.sh
	bash -n openevent-stack/stop.sh
	bash -n test.sh
