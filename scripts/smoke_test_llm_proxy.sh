#!/usr/bin/env bash
set -euo pipefail

PROXY_URL="${1:-http://localhost:18001}"
MODEL="${2:-qwen2.5-7b-instruct}"

echo "Running simple completion smoke test"
curl -fsSL "${PROXY_URL}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one sentence.\"}]}" \
  | jq -r '.choices[0].message.content'

echo "Running tool-call JSON validity smoke test"
RESP="$(curl -fsSL "${PROXY_URL}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Turn off all lights in kitchen.\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"discover_entities\",\"description\":\"Find entities in an area\",\"parameters\":{\"type\":\"object\",\"properties\":{\"area\":{\"type\":\"string\"}},\"required\":[\"area\"]}}},{\"type\":\"function\",\"function\":{\"name\":\"perform_action\",\"description\":\"Execute an action\",\"parameters\":{\"type\":\"object\",\"properties\":{\"entity_id\":{\"type\":\"string\"},\"action\":{\"type\":\"string\"}},\"required\":[\"entity_id\",\"action\"]}}}],\"tool_choice\":\"auto\"}")"

echo "${RESP}" | jq -e '.choices[0].message.tool_calls // []' >/dev/null

echo "Smoke test passed"
