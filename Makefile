PYTHON ?= python3

.DEFAULT_GOAL := build
unexport PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE := 1
export PIP_CACHE_DIR := $(CURDIR)/build/pip-cache
export TMPDIR := $(CURDIR)/build/tmp

.PHONY: build install clean

build install:
	"$(PYTHON)" -B scripts/build_package.py $@

clean:
	rm -rf build/package build/tmp build/pip-cache
	rm -f dist/openevent_agent_demo-*.whl dist/openevent_agent_demo-*.tar.gz
