#!/bin/sh
# Claude Code, stdio (recommended; the token stays in client.json/token files):
claude mcp add hive -- "$(command -v hive)" mcp --connection home --wait-budget 110

# Claude Code over Streamable HTTP to the hub's /mcp (the only case where a token appears in an agent config):
# claude mcp add --transport http hive "https://home-hub.tail1234.ts.net/mcp?wait_budget=110" \
#   --header "Authorization: Bearer $(cat ~/.hive/tokens/home.token)"
