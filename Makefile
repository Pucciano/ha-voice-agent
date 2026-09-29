.PHONY: dev-up dev-up-debug dev-down prod-up prod-down lint format test eval \
	sat-config sat-compile sat-flash sat-diag sat-logs sat-chip sat-xvf3800

ESPHOME_VERSION := 2026.9.0
ESPHOME := uvx --from esphome==$(ESPHOME_VERSION) esphome
SAT ?= living-room
SAT_CONFIG := esphome/aivi-sat-$(SAT).yaml
# Diagnostic build of the same device (docs/satellite_flashing.md).
SAT_DIAG_CONFIG := esphome/aivi-sat-$(SAT)-diagnostics.yaml
# DEVICE=/dev/cu.usbmodemXXXX for USB, an IP address or OTA for network.
SAT_DEVICE = $(if $(DEVICE),--device $(DEVICE))

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
	pylint --rcfile=.pylintrc services/llm_proxy/app training/eval training/llm training/stt scripts/*.py

format:
	black --line-length 80 services/llm_proxy/app training/eval training/llm training/stt scripts/*.py

test:
	python -m compileall services/llm_proxy/app training/eval training/llm training/stt scripts

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
