# MCP (`mcp-assist`) integration guide

This guide describes using `mcp-assist` with the local LLM endpoint.

## 1) Objective

Enable robust tool-calling from Home Assistant and MCP clients using:

- OpenAI-compatible endpoint (`/v1/chat/completions`)
- deterministic tool schema handling
- JSON validity repair in `llm_proxy`

## 2) Endpoint selection

- Preferred for MCP in development: `http://<host>:18001/v1`
- Direct vLLM (less guard rails): `http://<host>:10100/v1`

## 3) Tool schema strategy

Use explicit JSON Schema with required fields for every tool:

- `discover_entities`
- `perform_action`
- `get_entity_details`

Rules:

1. Keep schemas small and strict.
2. Avoid nullable-anything fields unless needed.
3. Include examples in tool descriptions.

## 4) Recommended system prompt pattern

```text
You are an assistant that controls Home Assistant entities via tools.
Policy:
1) For control requests, call discover_entities first.
2) Then call perform_action only on discovered entities.
3) Tool arguments must always be strict JSON (RFC8259).
4) If information is missing, ask a concise clarification question.
5) Never invent tools or entities.
```

## 5) llm_proxy behavior

`llm_proxy` adds:

- `/v1/models` fallback when upstream is unreachable
- `/v1/chat/completions` passthrough
- tool-call argument JSON validation
- one repair retry if invalid JSON is detected
- structured capture JSONL for prompts/responses/tool calls

## 6) Validation workflow

1. Send tool-heavy prompts to `llm_proxy`.
2. Inspect captured dataset rows under `dev/datasets/llm/`.
3. Run evaluators:

```bash
python training/eval/eval_tool_call_validity.py --dataset dev/datasets/llm
python training/eval/eval_two_step_compliance.py --dataset dev/datasets/llm
python training/eval/eval_latency_tokens.py --dataset dev/datasets/llm
```

## 7) Operational notes

- Keep production logs minimal (use `compose/prod/docker-compose.yml`).
- Use `compose/dev/docker-compose.override.yml` for high-verbosity diagnostics.
- For consistent behavior, pin tool schemas and model versions.
