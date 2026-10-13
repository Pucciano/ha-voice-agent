# LLM assistant

The first voice pipeline, "AIVI Lokal Deutsch", recognises speech with
Speech-to-Phrase and answers with Home Assistant's built-in agent. Both only
know fixed sentences. The LLM pipeline, "AIVI Cloud", understands free
speech: "Wie spät ist es?", "Wie viel Uhr ist es?" and "Sag mir mal die
Uhrzeit" are the same request. Issue #18 tracks it.

## Stage 1: a cloud model behind the Jetson

```text
Satellite -> Home Assistant, pipeline "AIVI Cloud"
  speech to text  Wyoming <jetson>:10310  stt-capture-cloud -> stt-bridge -> Mistral Voxtral
  agent           llama.cpp integration   <jetson>:18001 llm-proxy -> Mistral chat model
                  ("prefer handling commands locally": built-in sentences first)
  text to speech  Wyoming <jetson>:10200  Piper (unchanged)
```

- **Speech to text changes too.** Speech-to-Phrase maps every utterance onto
  its sentence grammar, so an LLM behind it would never see free speech.
  Whisper on the Nano's CPU takes 5–6 s (`base`) or 13–14 s (`small`) per
  command ([measurement](jetson_setup.md#why-this-stack)), so stage 1
  transcribes in the cloud.
- **Provider: Mistral AI**, hosted in the EU. One key serves the chat model
  and Voxtral Mini Transcribe. Its models have open weights, so the same
  family can later run locally.
- **The Jetson is the seam.** Home Assistant talks only to the Jetson: to the
  proxy with the fixed model name `aivi`, and to the bridge over Wyoming. The
  provider key lives only in the Jetson's `.env`. Moving to a local model or
  a local Whisper server changes an `.env` entry on the Jetson, not Home
  Assistant ([section 11](jetson_setup.md#change-model-or-provider)).
- **Home Assistant's own `llama.cpp` integration** (core since 2026.8) talks
  to any OpenAI-compatible server and hands the model Home Assistant's Assist
  tools: turning devices on and off, lights, timers, lists, media, weather,
  date and time, and the live states of the exposed entities. No custom
  integration is needed.
- **"Prefer handling commands locally"** stays on: a sentence that the
  built-in agent knows is executed at once, without a model call; everything
  else goes to the LLM.
- **Data**: the transcription and the chat model see what was said after the
  wake word, plus the names, areas and states of the entities exposed to
  Assist. The proxy and the second relay keep training data on the Jetson's
  capture drive ([section 9](jetson_setup.md#9-request-capture),
  [section 11](jetson_setup.md#11-llm-proxy)).
- **Offline**: without internet the LLM pipeline fails at once: speech to
  text ends with `stt-stream-failed` within about 0.1 s, and the LED ring
  shows the error. If only the model is down, Home Assistant speaks the
  proxy's message "Das Sprachmodell ist gerade nicht erreichbar." The
  Speech-to-Phrase pipeline stays configured; switching the satellite's
  "Assistent" back to it restores voice control (tested on 2026-10-12 with an
  unresolvable provider host).

## Set up

1. Mistral account: create an API key in the console and set a monthly
   spending limit.
2. Jetson: keys, images and compose file as in
   [section 11](jetson_setup.md#11-llm-proxy), then check the proxy and the
   bridge ([section 12](jetson_setup.md#12-cloud-speech-to-text)).
3. Home Assistant, **Settings → Devices & services → Add integration**:
   - **Wyoming Protocol**, host `<jetson-ip>`, port `10310` (the cloud
     speech-to-text).
   - **llama.cpp**, server URL `http://<jetson-ip>:18001/v1`, API key: the
     proxy key, model `aivi`. Home Assistant finds no streaming and turns it
     off.
4. Configure the llama.cpp agent (the gear on its entry):
   - Instructions: the text of
     `config/homeassistant/llm_assistant_instructions.de.txt`.
   - Control Home Assistant: **Assist**.
   - Untick "recommended settings" and set the temperature to 0.3; leave the
     rest.
5. **Settings → Voice assistants → Add assistant** "AIVI Cloud": language
   German, conversation agent the llama.cpp agent with "Prefer handling
   commands locally" on, speech to text the Wyoming entry on port 10310
   (German), text to speech Piper with `de_DE-thorsten-medium`.
6. On the satellite's device page, set **Assistent** to "AIVI Cloud". The
   old pipeline stays as it is.

## Choose the model

`training/eval/eval_assist_agent.py` compares chat models on the real Assist
tools without executing anything. It takes the system prompt and the tools
from one request that Home Assistant sent through the proxy, replaces the
sentence with each case of `training/eval/assist_agent_cases.de.json` and
checks the model's first step: the tool and its arguments, or a plain
answer. The cases are paraphrases of everyday commands plus misheard
fragments that must not switch anything.

It runs in the proxy's container on the Jetson, where the key is. Ask
something through the new pipeline first, so that a capture file exists,
then copy the script and the cases:

```bash
scp training/eval/eval_assist_agent.py training/eval/assist_agent_cases.de.json aivi@<jetson-ip>:/tmp/aivi-update/
```

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose run --rm --no-deps -T -v /tmp/aivi-update:/eval:ro llm-proxy sh -c "python /eval/eval_assist_agent.py --template \$(ls /capture/drive/aivi-llm/llm_capture_*.jsonl | tail -n 1) --cases /eval/assist_agent_cases.de.json --model mistral-small-latest --model ministral-8b-latest"'
```

Each model gets one line with passes, median time and tokens per step;
`--price MODEL=IN/OUT` adds the cost. The timer cases are skipped with a
template from the API, because Home Assistant offers timer tools only to a
device that supports timers.

Result on 2026-10-12 (Home Assistant's prompt with 74 exposed entities, about
4,900 tokens, 20 tools):

| Model | Passed | Median per step | Notes |
|---|---:|---:|---|
| `mistral-small-latest` (Small 4) | 72/72 | 0.56 s | chosen; first run without the two action rules: 14/20, it read the state before every switch |
| `ministral-8b-latest` | 17/20 | 0.55 s | switched "Licht im Wohnzimmer" by an unnamed bulb, invented "Flurlampe" |
| `ministral-14b-latest` | 20/20 | 0.78 s | splits room commands into one call per device, up to 3.7 s |
| `mistral-medium-latest` (3.5) | 20/20 | 1.26 s | clean calls, about ten times the price of Small |

The instructions carry what the runs taught:

- The time from `GetDateTime` is local time. Without saying so, Small
  "converted" 23:51 CEST into 21:52.
- A switch command calls the tool directly, a room command names the room.
- A statement is no command. "Der Trockner läuft nicht mehr" switched the
  dryer's plug off before this rule; now the agent asks.

## Measured

Voice runs on 2026-10-12 with the living room satellite, from the end of
speech (voice activity detection) to the text of the answer; Piper adds about
1 s on top while the answer is played:

| Step | Time |
|---|---|
| Speech to text, Voxtral through the bridge | 0.5–0.8 s (one 1.8 s) |
| Sentence the built-in agent knows ("prefer handling commands locally") | 0.01–0.02 s |
| LLM answer, one tool call | 0.9–1.7 s |
| End of speech to answer text | 0.5–2.2 s |

For comparison, Speech-to-Phrase needs about 1 s and Whisper on the Nano 5 to
14 s. Voxtral also wrote "Das war's." where Speech-to-Phrase had heard
"schalt das Konsole aus" and switched the Jetson off.

## Known limits

- **Timers:** they work, since the satellite announces timer support
  ([satellite runbook, step 8](satellite_flashing.md#8-voice-timers)). A
  misheard sentence can still name a timer wrongly: Voxtral once wrote "Ich
  stelle einen Timer auf eine Minute", and Home Assistant's built-in sentence
  took "Ich stelle einen" as the timer's name.
- **Follow-up questions:** when the agent asks back ("Soll ich ihn
  ausschalten?"), the answer needs the wake word again, because the satellite
  does not continue the conversation.
- **Appliance plugs:** for Assist, a plug that is on means "running", and
  "Der Trockner läuft nicht mehr" once switched the dryer's plug off. The
  plugs of the dryer, the washing machine and the dishwasher are therefore
  not exposed. Instead, three read-only template binary sensors with device
  class "running" (`binary_sensor.dryer_running`,
  `binary_sensor.washing_machine_running`,
  `binary_sensor.dishwasher_running`, named "Trockner", "Waschmaschine",
  "Spülmaschine") mirror the run helpers that the power-based automations
  keep, and they are exposed. "Läuft der Trockner noch?" now gets the
  machine's state, and no misheard sentence can cut a running machine.
- **Weather:** the weather tool only gives the current conditions, no
  forecast.

## Costs

A command with a tool takes two model requests of about the size of Home
Assistant's prompt with the exposed devices (about 5,000 tokens, nearly all of
it served from the provider's cache), plus a few seconds of transcription. At
the prices of October 2026 (Small 4 about $0.15/$0.60 per million tokens,
Voxtral Mini Transcribe $0.003 per minute) that is about 0.2 US cents per
command even without the cache. The proxy's daily limit (500 requests) caps a runaway loop;
the spending limit in the Mistral console caps everything.
