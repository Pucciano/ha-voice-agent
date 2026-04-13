# Home Assistant integration guide

This guide wires Home Assistant to the local `ha-voice-agent` stack, including
the inference services, voice satellites, and audio output.

## 1) Prerequisites

Before configuring Home Assistant, the following must be in place:

- Docker and the Compose plugin running on the inference server.
- The NVIDIA Container Toolkit installed for GPU passthrough.
- The ha-voice-agent services running and healthy:
  - STT (Wyoming Faster-Whisper) on port 10300
  - TTS (Wyoming Piper) on port 10200
  - LLM endpoint on port 10100 (vLLM direct) or 18001 (llm_proxy in dev)
- Voice satellites flashed and connected to the network (see `docs/satellite_setup.md`).
- Bluesound Pulse Flex speakers powered on and discoverable (see `docs/audio_output.md`).

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
- Model: `aivi-8b-instruct` (or your configured served model name)

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

## 8) Voice satellite integration

Once the ESP32S3 satellites are flashed with the ESPHome firmware and connected
to the network, they should appear automatically in Home Assistant through the
ESPHome integration. Each satellite registers as a device with voice assistant
capabilities. Assign each satellite to the voice pipeline that uses the local
STT and TTS integrations configured above, and set the audio output to the
Bluesound media_player entity in the same room. This way, the wake word
detected on the satellite triggers the full pipeline, and the spoken response
plays back on the Pulse Flex rather than through the satellite hardware.

For detailed satellite hardware setup and firmware configuration, refer to
`docs/satellite_setup.md`. For Bluesound speaker configuration and TTS audio
routing, see `docs/audio_output.md`.

## 9) Troubleshooting

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
