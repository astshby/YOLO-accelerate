PYTHON ?= python3
VERILATOR ?= verilator
MODEL ?= yolox-tiny
ADAM_BUILD ?= build/adam128
NPU_BUILD ?= build/npu128
MMIO_BUILD ?= build/mmio
UART_BUILD ?= build/uart
TORCH_PYTHON ?= python3
IMAGE ?= build/upstream/YOLOX/assets/dog.jpg
OUTPUT ?= build/dog-adam128.png
SIZE ?= 416
ADAM_RTL = rtl/adam/adam_mul_pair.sv rtl/adam/adam_sdp_ram.sv rtl/adam/adam_mac128.sv rtl/adam/adam_conv128.sv
NPU_RTL = $(ADAM_RTL) rtl/npu/npu_postprocess.sv rtl/npu/npu_conv128.sv rtl/npu/npu_measure_top.sv
MMIO_RTL = $(ADAM_RTL) rtl/npu/npu_postprocess.sv rtl/npu/npu_conv128.sv rtl/npu/npu_mmio128.sv
UART_RTL = $(MMIO_RTL) rtl/io/uart_8n1.sv rtl/io/uart_mmio_bridge.sv rtl/board/npu_uart_board.sv

.PHONY: test lint upstream-test upstream-lint adam-test adam-lint npu-test npu-lint mmio-test mmio-lint uart-test uart-lint tile-test audit audit-both image-sim image-test image-detect clean
.NOTPARALLEL: test lint
test: upstream-test

upstream-test:
	$(PYTHON) tools/run_upstream_tests.py

lint: upstream-lint

upstream-lint:
	$(PYTHON) tools/run_guarded.py --rss-mib 2048 --as-mib 3072 --min-available-mib 6144 --log build/upstream-faithful/lint.log --metrics build/upstream-faithful/lint.metrics.json -- $(VERILATOR) --lint-only -Wall -Wno-fatal --top-module adam_upstream rtl/upstream/adam_upstream.sv $(wildcard third_party/adamgallas/source/*.v)

npu-test:
	$(PYTHON) tools/run_npu_tests.py --verilator $(VERILATOR) --build-dir $(NPU_BUILD)

npu-lint:
	$(PYTHON) tools/run_guarded.py --log $(NPU_BUILD)/lint.log --metrics $(NPU_BUILD)/lint.metrics.json -- $(VERILATOR) --lint-only -Wall --top-module npu_measure_top $(NPU_RTL)

mmio-test:
	$(PYTHON) tools/run_mmio_tests.py --verilator $(VERILATOR) --build-dir $(MMIO_BUILD)

mmio-lint:
	$(PYTHON) tools/run_guarded.py --min-available-mib 6144 --log $(MMIO_BUILD)/lint.log --metrics $(MMIO_BUILD)/lint.metrics.json -- $(VERILATOR) --lint-only -Wall --top-module npu_mmio128 $(MMIO_RTL)

uart-test:
	$(PYTHON) tools/run_uart_tests.py --build-dir $(UART_BUILD)

uart-lint:
	$(PYTHON) tools/run_guarded.py --min-available-mib 6144 --log $(UART_BUILD)/lint.log --metrics $(UART_BUILD)/lint.metrics.json -- $(VERILATOR) --lint-only -Wall --top-module npu_uart_board $(UART_RTL)

adam-test:
	$(PYTHON) tools/run_adam_tests.py --verilator $(VERILATOR) --build-dir $(ADAM_BUILD)

adam-lint:
	$(PYTHON) tools/run_guarded.py --log $(ADAM_BUILD)/lint.log --metrics $(ADAM_BUILD)/lint.metrics.json -- $(VERILATOR) --lint-only -Wall --top-module adam_conv128 $(ADAM_RTL)

tile-test:
	$(PYTHON) tools/run_guarded.py --log build/sim/guard.log --metrics build/sim/guard.metrics.json -- $(PYTHON) tools/run_rtl_tests.py --verilator $(VERILATOR) --jobs 1

image-sim:
	$(PYTHON) tools/build_image_sim.py --verilator $(VERILATOR)

image-test: image-sim
	$(PYTHON) tools/run_guarded.py --log build/image-cosim/backend-test.log --metrics build/image-cosim/backend-test.metrics.json -- $(PYTHON) tb/check_image_backend.py

image-detect: image-sim
	$(PYTHON) tools/run_guarded.py --rss-mib 1024 --as-mib 16384 --min-available-mib 4096 --seconds 600 --log "$(OUTPUT).run.log" --metrics "$(OUTPUT).metrics.json" -- $(TORCH_PYTHON) tools/detect_image.py --image "$(IMAGE)" --output "$(OUTPUT)" --size $(SIZE)

audit:
	$(PYTHON) tools/audit_yolox.py --source build/upstream/YOLOX --model $(MODEL) --output build/$(MODEL)-audit.json

audit-both:
	$(MAKE) audit MODEL=yolox-nano
	$(MAKE) audit MODEL=yolox-tiny
	$(PYTHON) tools/compare_models.py --nano build/yolox-nano-audit.json --tiny build/yolox-tiny-audit.json --output build/model-comparison.json

clean:
	rm -rf build/sim build/adam128 build/npu128 build/image-cosim
