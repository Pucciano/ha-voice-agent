.PHONY: dev-up dev-up-debug dev-down prod-up prod-down lint format test eval \
	ww-setup ww-preview ww-samples ww-record ww-record-negative ww-import \
	ww-features ww-train ww-evaluate

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
