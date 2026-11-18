#!/bin/sh
# Hive over plain HTTP (design §6, §8.7, §22.4). Any language works the same way.
#   HIVE_URL=http://127.0.0.1:7701  HIVE_TOKEN_FILE=~/.hive/tokens/this-pc.token  sh raw_http.sh
set -eu
URL=${HIVE_URL:-http://127.0.0.1:7701}
TOKEN=$(head -n 1 "${HIVE_TOKEN_FILE:-$HOME/.hive/tokens/this-pc.token}")   # never put tokens on the command line of shared machines

# 1. one action: POST /v1/call (on a hub add "node": "desk-wsl")
curl -fsS "$URL/v1/call" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action": "shell.run", "params": {"command": "uname -a", "timeout": 30}}'
echo

# 2. tool definitions for your own agent loop (formats: mcp, anthropic, openai, openai-responses)
curl -fsS "$URL/v1/tools?format=openai&profile=minimal" -H "Authorization: Bearer $TOKEN" | head -c 600
echo

# 3. the model gateway: an OpenAI-compatible endpoint for a model served on a node (key = a gateway token from
#    `hive gateway token add APP --models 'desk-wsl/qwen'`; base URLs from `hive gateway url desk-wsl qwen`)
if [ -n "${HIVE_GATEWAY_TOKEN:-}" ]; then
  curl -fsS "${HIVE_GATEWAY_BASE:-http://127.0.0.1:7700/openai/desk-wsl/qwen/v1}/chat/completions" \
    -H "Authorization: Bearer $HIVE_GATEWAY_TOKEN" -H "Content-Type: application/json" \
    -d '{"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 32}'
  echo
fi
