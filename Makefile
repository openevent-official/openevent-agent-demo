PYTHON ?= python3
TEST_ARGS ?=

.PHONY: test check-scripts

test: check-scripts
	PYTHON="$(PYTHON)" ./test.sh $(TEST_ARGS)

check-scripts:
	bash -n scripts/bootstrap_ubuntu_venv.sh
	bash -n openevent-stack/bootstrap.sh
	bash -n openevent-stack/common.sh
	bash -n openevent-stack/logs.sh
	bash -n openevent-stack/process.sh
	bash -n openevent-stack/render-view-config.sh
	bash -n openevent-stack/start.sh
	bash -n openevent-stack/status.sh
	bash -n openevent-stack/stop.sh
	bash -n test.sh
