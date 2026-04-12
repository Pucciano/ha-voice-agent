.PHONY: dev-up dev-up-debug dev-down prod-up prod-down lint format test eval

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
