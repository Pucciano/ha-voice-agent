# Home Assistant integration guide

This guide wires Home Assistant to the local `ha-voice-agent` stack.

## 1) Prerequisites

- Docker + Compose working on the host.
- NVIDIA Container Toolkit installed for GPU usage.
- `ha-voice-agent` services running:
  - STT (Wyoming Faster-Whisper) on `10300`
  - TTS (Wyoming Piper) on `10200`
  - LLM endpoint on `10100` (vLLM) or `18001` (llm_proxy)

## 2) Start stack

For development:

```bash
docker compose -f compose/dev/docker-compose.yml up -d
```

For production:

```bash
docker compose -f compose/prod/docker-compose.yml up -d
```

## 3) Configure Wyoming STT in Home Assistant

1. Open **Settings -> Devices & Services -> Add Integration**.
2. Add **Wyoming Protocol**.
3. Enter host/IP of your `ha-voice-agent` machine and port `10300`.
4. Name suggestion: `Local Faster-Whisper STT`.

## 4) Configure Wyoming TTS in Home Assistant

1. Add another **Wyoming Protocol** integration.
2. Use same host/IP and port `10200`.
3. Name suggestion: `Local Piper TTS`.

## 5) Configure Conversation LLM endpoint

Choose one target:

- **Direct vLLM:** `http://<host>:10100/v1`
- **Recommended dev capture path:** `http://<host>:18001/v1`

In Home Assistant's LLM/OpenAI-compatible integration:

- Base URL: one of the above
- API key: can be any non-empty placeholder if HA requires one
- Model: `qwen2.5-7b-instruct` (or your configured served model name)

## 6) Suggested system prompt

Use this as a baseline and adapt to your entity naming:

```text
You are a Home Assistant control assistant.
When controlling devices, first call discover_entities with the user's area/name.
Only after discovering entities, call perform_action.
Always return valid JSON for tool arguments.
Never hallucinate unavailable tools or entities.
```

## 7) Quick verification

- Ask HA: "Turn off kitchen lights."
- Confirm:
  - Faster-Whisper receives STT traffic (port 10300)
  - Piper responds with TTS audio (port 10200)
  - LLM endpoint returns tool calls

For dev capture mode, verify JSONL files appear in:

- `dev/datasets/llm/`
- `dev/datasets/stt/` (when using ingest workflow)

## 8) Troubleshooting

- If STT/TTS integrations do not connect, verify host firewall rules.
- If LLM tool calls are malformed, route HA to `llm_proxy` on `18001`.
- If vLLM logs repeatedly show `NotImplementedError` from
  `openai_tool_parser.py` while returning HTTP 200, your client is likely
  using **streaming tool calls** against direct vLLM (`:10100`).
  - Prefer `llm_proxy` on `:18001` for tool-calling flows.
  - Disable streaming in the HA/OpenAI integration (`stream=false`) when using
    tool calls.
- If GPU is unavailable in containers, run:

```bash
scripts/check_gpu_docker.sh
```
