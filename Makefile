.PHONY: dev-up dev-up-debug dev-down prod-up prod-down lint format test eval \
	sat-config sat-compile sat-flash sat-diag sat-logs sat-chip sat-xvf3800 \
	ww-setup ww-preview ww-samples ww-record ww-record-negative ww-import \
	ww-features ww-train ww-evaluate

ESPHOME_VERSION := 2026.9.0
ESPHOME := uvx --from esphome==$(ESPHOME_VERSION) esphome
SAT ?= living-room
SAT_CONFIG := esphome/aivi-sat-$(SAT).yaml
# Diagnostic build of the same device (docs/satellite_flashing.md).
SAT_DIAG_CONFIG := esphome/aivi-sat-$(SAT)-diagnostics.yaml
# DEVICE=/dev/cu.usbmodemXXXX for USB, an IP address or OTA for network.
SAT_DEVICE = $(if $(DEVICE),--device $(DEVICE))

# Wake word training, see docs/wake_word_training.md. HOST is the
# satellite's IP address, RUN a training run name (default: new or latest).
WW := uv run --project training/wake_word python training/wake_word
TAKES ?= 1

dev-up:
	docker compose -f compose/dev/docker-compose.yml up -d

dev-up-debug:
	docker compose -f compose/dev/docker-compose.yml -f compose/dev/docker-compose.override.yml up -d

dev-down:
	docker compose -f compose/dev/docker-compose.yml down

prod-up:
	docker compose -f compose/prod/docker-compose.yml up -d

prod-down:
	docker compose -f compose/prod/docker-compose.yml down

lint:
	pylint --rcfile=.pylintrc services/llm_proxy/app training/eval training/llm training/stt training/wake_word scripts/*.py

format:
	black --line-length 80 services/llm_proxy/app training/eval training/llm training/stt training/wake_word scripts/*.py

test:
	python -m compileall -q -x '/\.venv/' services/llm_proxy/app training/eval training/llm training/stt training/wake_word scripts

eval:
	python training/eval/eval_tool_call_validity.py --dataset dev/datasets/llm

sat-config:
	$(ESPHOME) config $(SAT_CONFIG)

sat-compile:
	$(ESPHOME) compile $(SAT_CONFIG)

sat-flash:
	$(ESPHOME) run $(SAT_CONFIG) $(SAT_DEVICE)

sat-diag:
	$(ESPHOME) run $(SAT_DIAG_CONFIG) $(SAT_DEVICE)

sat-logs:
	$(ESPHOME) logs $(SAT_CONFIG) $(SAT_DEVICE)

sat-chip:
	uvx --from esphome==$(ESPHOME_VERSION) python -m esptool --port $(DEVICE) flash-id

sat-xvf3800:
	scripts/flash_xvf3800.sh

ww-setup:
	$(WW)/download.py

ww-preview:
	$(WW)/generate_samples.py --preview

ww-samples:
	$(WW)/generate_samples.py

ww-record:
	$(WW)/record_satellite.py --host $(HOST) --speaker $(SPEAKER) --takes $(TAKES) \
		$(if $(DURATION),--seconds $(DURATION))

ww-record-negative:
	$(WW)/record_satellite.py --host $(HOST) --negative --takes $(TAKES) \
		$(if $(DURATION),--seconds $(DURATION))

ww-import:
	$(WW)/import_recordings.py

ww-features:
	$(WW)/build_features.py

ww-train:
	$(WW)/train.py $(if $(RUN),--run $(RUN))

ww-evaluate:
	$(WW)/evaluate.py $(if $(RUN),--run $(RUN))
