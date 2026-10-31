SHELL := /bin/bash
PYTHON ?= python3
T ?=

.PHONY: help mirror setup fetch-tests fetch-lib isa demo app bench cocotb-setup cocotb riscv-dv synth opensta sta clean all

help:
	@echo "Sprouts500 targets:"
	@echo "  make setup        install Ubuntu host tools"
	@echo "  make fetch-tests  fetch riscv-arch-test 2.x sources"
	@echo "  make fetch-lib    fetch Nangate45 Liberty"
	@echo "  make isa          run all RV32I/RV32M ISA tests (T=name for one)"
	@echo "  make demo         build and run the bare-metal SoC demo"
	@echo "  make app APP=dir  build and run a user C program (e.g. sw/example)"
	@echo "  make bench        run CoreMark and Dhrystone (BENCH=coremark|dhrystone)"
	@echo "  make cocotb-setup one-time cocotb venv (python3.14-dev)"
	@echo "  make cocotb       run cocotb SoC + core suites"
	@echo "  make riscv-dv     riscv-dv random lockstep vs Sail (seeds 1 2 3)"
	@echo "  make synth        Yosys gate-level synthesis"
	@echo "  make opensta      install CUDD + OpenSTA system-wide (one time, sudo)"
	@echo "  make sta          run the OpenSTA timing report"
	@echo "  make clean        remove build/ and logs/"

mirror:
	bash scripts/setup_apt_mirror.sh

setup:
	bash scripts/setup_ubuntu.sh

fetch-tests:
	$(PYTHON) scripts/fetch_isa.py

fetch-lib:
	bash scripts/fetch_lib.sh

isa:
	$(PYTHON) scripts/run_isa.py $(T)

demo:
	bash scripts/build_demo.sh

app:
	@if [ -z "$(APP)" ]; then echo "usage: make app APP=<dir> [ARGS='+VCD']"; exit 2; fi
	bash scripts/build_app.sh $(APP) $(ARGS)

bench:
	bash scripts/build_bench.sh $(BENCH)

cocotb-setup:
	bash scripts/setup_cocotb.sh

cocotb:
	bash scripts/run_cocotb.sh

riscv-dv:
	bash scripts/setup_riscv_dv.sh
	bash scripts/run_riscv_dv.sh $(SEEDS)

synth:
	bash scripts/synth.sh

opensta:
	sudo -E bash scripts/setup_opensta.sh

sta:
	bash scripts/sta.sh

clean:
	rm -rf build logs

all: isa demo app synth
