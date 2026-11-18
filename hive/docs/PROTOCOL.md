# Hive wire and relay protocol (reference)

This is the protocol reference for clients and for anyone writing another Hive implementation. It is a copy of
DESIGN.md §6–§7 and §8.7 (v8), kept in sync with it; DESIGN.md is authoritative when the two differ.

## 1. Transport (§6.1)

- HTTP/1.1 + JSON, keep-alive (≤ 100 requests per connection), `Server: hive`.
- Auth: `Authorization: Bearer <token>` only (never in a query string). Tokens start with `hv_`. Missing or wrong →
  401 + `WWW-Authenticate: Bearer`; 10 failures / 60 s from one IP → 429 + `Retry-After` (reason `auth_throttle`).
- Headers must arrive within 10 s; authentication happens from the headers before any body byte is read, so an
  unauthenticated request with a body is answered 401 without reading it.
- POST needs `Content-Length` (chunked → 411) and `Content-Type: application/json` (→ 415). Body limit 48 MiB
  (`listen.max_body_bytes`), gateway routes `gateway.max_body_bytes` (8 MiB) → 413. JSON nested deeper than 64 →
  400.
- `Origin` not in `allowed_origins` → 403 (except `/openai/*` with `gateway.cors`). Unexpected `Host` → 421.
- Optional request header `X-Hive-Api: 1`; another value → 400 "api mismatch". Every response carries
  `X-Hive-Api: 1`, `X-Request-Id`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.
- Plain `http://` with a token is allowed only to loopback, private/tailnet addresses (rule C1); everything else is
  `https://` with system CAs, a `ca_file`, or a pin `tls_pin: "sha256:<hex of the DER certificate>"`, compared
  before any request byte is sent (mismatch → `unavailable` reason `tls_pin`, never retried).

## 2. Envelopes (§6.2)

Success (200):
```json
{"ok": true, "result": {"...": "..."},
 "meta": {"request_id": "r_…", "node": "desk", "action": "shell.run", "duration_ms": 812, "via": "hub:home", "api": 1}}
```
Error (HTTP status by code: bad_request 400, unauthorized 401, forbidden/policy_denied 403, not_found 404,
conflict 409, too_large 413, approval_required 428, rate_limited 429, cancelled 499, internal 500, unsupported 501,
unavailable 503, timeout 504):
```json
{"ok": false, "error": {"code": "policy_denied", "message": "…", "hint": "…", "details": {"rule": "fs_roots"}},
 "meta": {"request_id": "r_…", "api": 1}}
```
- A result that secret masking changed carries `"masked": true` at the top level (and `meta.masked`).
- `unavailable` has `details.reason` ∈ `offline|busy|not_polling|lost|session_replaced|identity_changed|connect|dns|
  proxy|proxy_auth|tls_pin|upstream` and an explicit `details.maybe_executed`; `timeout` has `maybe_executed`;
  `rate_limited` has `retry_after` and `reason` ∈ `auth_throttle|inflight|queue_full|jobs_running|pulls_running|
  approvals_pending|dist`.
- Domain outcomes are not errors (non-zero exit codes, HTTP 500 from `net.http`, `exists:false`, …).

## 3. Endpoints (§6.3)

| Method & path | Auth | Server | Body → result |
|---|---|---|---|
| `GET /v1/health` | none | both | `{"role":"node"\|"hub","api":1}` |
| `GET /v1/whoami` | token | both | `{"identity":{…},"server":{"role","name","version","api"},"approvals":bool}` |
| `GET /v1/actions[?node=N]` | token | both | `{"node","catalog_hash","implemented"(node only),"actions":[…]}`; hub without `node`: union with per-action `nodes` |
| `GET /v1/nodes` | token | both | `{"nodes":[NodeSummary]}` |
| `POST /v1/call` | token | both | §4 |
| `POST /v1/cancel` | token | both | `{"request_id"}` → `{"cancelled":bool}` (own requests; owner: any) |
| `GET /v1/tools?format=&profile=&prefix=&node=` | token | both | tool exports (mcp, anthropic, openai, openai-responses) |
| `GET /v1/approvals`, `POST /v1/approvals/{id}/approve\|deny`, `GET /v1/approvals/grants`, `POST /v1/approvals/grants/revoke` | owner (loopback) | both | approval records / grants |
| `POST /v1/hub/nodes/{name}/restart` | owner | hub | `{}` → `{"queued":bool}` (relay nodes only; next poll gets `refresh:"restart"`) |
| `POST /v1/hub/nodes/{name}/ack` | owner | hub | `{"pending_sha256"?}` → `{"bound","pending","refused"}` (§5.4 below) |
| `POST /v1/relay/register` · `/poll` · `/result` | relay node token | hub | §5 |
| `GET /v1/dist/hive.pyz` | none (≤ 6/min per IP, ≤ 4 concurrent) | hub | zipapp bytes, header `X-Hive-Sha256` |
| `GET\|POST /openai/{node}/{service}/[v1/]{models,chat/completions,completions,embeddings}` | models (+ hub token on a node) | both | OpenAI-compatible gateway (§6) |
| `OPTIONS /openai/…` | none (when `gateway.cors`) | both | CORS preflight 204 |

Token kinds per route: `token` routes take owner/agent/hub tokens (a `models` token gets 403); `owner` routes take
the owner token from an allowed source (loopback by default); `relay` routes take only relay node tokens; `/openai`
takes `models` tokens (and, on a node, its `kind:hub` tokens).

## 4. `POST /v1/call` (§6.4, §6.5)

```json
{"action": "shell.run", "params": {"command": "nvidia-smi"}, "node": "gpu-box", "timeout": 90,
 "request_id": "r_…", "approval_id": "ap_…", "bound_files": {"/home/me/run.sh": "sha256:…"}}
```
- `node`: on a hub a node name, `node_id` or alias (case-insensitive); omitted → the token's only node, else
  `default_node`, else 400 listing the nodes the token may see. On a node: absent, `"self"` or its own name.
  node_ids (audit round 3): a direct node's id is the one its authenticated `node.info` reports to the hub's probe
  and is matched before the ids relay pods report at register; an id two links of the same kind report →
  `not_found` "ambiguous", hint "address the node by name".
- `request_id` `^[A-Za-z0-9_.:-]{1,80}$` (generated when missing) is used for cancel, audit and correlation.
- Deadline = `timeout` or `catalog.deadline(spec, params)`, clamped to `[1, 3700]`; the hub waits 15 s more.
- `bound_files` only from a `kind:hub` token (any other caller → 400): the fingerprints of a hub approval; the node
  verifies and holds those files before the handler runs (a changed file → 409).
- Retries: a client resends a POST (same `request_id`, ≤ 2 times) only when no request byte was written (connect,
  DNS, TLS handshake, proxy CONNECT failed). Any later failure of a mutating call is `unavailable`/`timeout` with
  `maybe_executed: true`.
- The hub never forwards an `approval_id`; it rewrites a node's `approval_required` into `internal`.
- Hub-only agent fields reach a node only through a `kind:hub` token: `X-Hive-Agent: <agent token name>`
  (informational) and `X-Hive-Exec-Gated: 1` (narrowing-only).

## 5. Relay protocol (§7)

All relay requests are `POST`, JSON, `Authorization: Bearer <relay token>`. The hub maps the token to exactly one
configured node; a body `node` that differs → 403.

### 5.1 Register — `POST /v1/relay/register`
```json
{"node": "pod-a100", "node_id": "n_…", "instance_key": "ik_…", "api": 1, "version": "1.0.0", "pid": 1234,
 "info": {"…node.info result…"}, "actions": ["fs.read", "…"], "catalog_hash": "…", "max_parallel": 8,
 "tags": ["ignored by the hub"]}
```
→ `{"session_id":"s_…","node":"pod-a100","poll_wait":25,"server_time":"…Z","max_result_bytes":33554432}`

- `session_id` = `"s_"` + 24 random bytes, URL-safe base64 (audit round 3; was a millisecond clock + 32 random bits).
- `node_id` authenticates nothing: it is dropped (warning audit record "relay node_id refused") when it equals a
  configured name or alias, the id of another link — including the id a direct node reports in its `node.info` to
  the hub's probe — or an id `relay_ids.json` binds to another node. Audit round 3: when a direct probe later learns
  an id a relay node already claimed, the claim (live session and `relay_ids.json` binding) is dropped ("relay
  node_id claim dropped"). The ids direct links learned are persisted in `state/direct_ids.json` (with the
  node's url and pin; a changed url or pin forgets the entry) and loaded at hub start, so they are refused from the
  first register after a restart, even while the direct node is offline.

- A new register supersedes the old session: delivered, unfinished tasks fail with `unavailable`
  `session_replaced` (`maybe_executed` true for mutating actions); queued tasks are kept.
- `info` + `actions` ≤ 2 MiB; every string in `info`/`status` is cut to 200 characters and stripped of control, bidi
  and zero-width characters; `tags`/`description` come only from the hub config.
- **Instance key (D54).** The node keeps `<data>/instance_key` (`ik_` + 32 random bytes, 0600). The hub stores its
  sha256 in `<hub data>/state/relay_ids.json`:
  `{"v":1,"nodes":{name:{"key_sha256","pending_sha256","pending":{"ip","at","hostname","pod_id"},"token_sha256",
  "bound_at","bound_seen_at"}}}`. No key bound yet → the first register binds it. The bound key → supersedes and
  clears any pending attempt (`bound_seen_at` = now). Another key while the node is online → 409; while offline →
  accepted, `identity_changed: true`, the key hash kept as `pending_sha256`, and nothing is routed to the node
  (`unavailable` reason `identity_changed`) until `POST /v1/hub/nodes/{name}/ack` with that exact
  `pending_sha256` (refused when the bound instance registered after the pending attempt). An entry whose
  `token_sha256` differs from the configured relay token (after `hive hub rotate-token`) is reset at hub start.

### 5.2 Poll — `POST /v1/relay/poll`
```json
{"session_id": "s_…", "running": ["t_…"], "free_slots": 6, "status": {"cpu_pct": 12.5, "…": "…"}}
```
The hub holds the request up to `poll_wait` until a task, a cancel or a refresh exists:
```json
{"tasks": [{"task_id": "t_…", "request_id": "r_…", "action": "shell.run", "params": {}, "timeout_s": 75.0,
            "agent": "claude-desktop", "exec_gated": false, "bound_files": null}],
 "cancel": ["t_…"], "refresh": false}
```
- At most `free_slots` (≤ 16) tasks; the response stays under `max_poll_bytes` (32 MiB) but carries at least one
  task. At most `max(1, max_parallel − 2)` `model.forward` tasks are delivered at a time (slot reserve).
- `timeout_s` is relative: the rest of the hub's call deadline without the hub's own slack.
- Only one poll per node is held; a newer poll makes the older one return empty. Unknown session → 409 (register
  again). 401/403 → the node backs off 300 s. Audit round 3: 409s count per (relay node, source IP) (`throttle` settings,
  10 in 60 s → 300 s); while that key is blocked, polls whose `session_id` is not the node's live session get 429
  `rate_limited` reason `auth_throttle` with `retry_after` (the node waits at least that long, at most 600 s); a
  poll with the live session always proceeds, so pods sharing one address (NAT egress, a proxy without
  `X-Forwarded-For`) cannot block each other; a successful register clears the node's count and block for that IP.
- `refresh: "restart"` → the node shuts down gracefully and exits 75 (its service manager starts it again).
- `status` (≤ 4 KiB) every 30 s.

### 5.3 Result — `POST /v1/relay/result`
`{"session_id","task_id","envelope":{…}}` → `{"accepted":true|false}` (false for unknown, superseded, finished or
expired tasks). Body ≤ `max_result_bytes` + 64 KiB; larger envelopes are replaced by the node with `too_large`. The
node retries at 1, 3, 9 s on network errors.

### 5.4 Hub-side outcomes (§7.4)

| Situation | Result |
|---|---|
| node offline (no poll held, last one ended > 75 s ago) | `unavailable` `offline`, `maybe_executed:false` |
| node `identity_changed` | `unavailable` `identity_changed`, `maybe_executed:false` |
| queue ≥ 64 | `rate_limited` `queue_full` |
| not delivered within `pickup_timeout` (20 s) | `unavailable` `not_polling` / `busy`, `maybe_executed:false`; never executed later |
| delivered, no result by `timeout_s + relay_grace` (10 s) | cancel queued; `timeout`, `maybe_executed` = mutating |
| delivered, missing from the next poll's `running` for `lost_grace` (10 s) | `unavailable` `lost`, `maybe_executed` = mutating |
| re-register | delivered tasks → `unavailable` `session_replaced` |

Delivery is at most once. Tasks run on the node as the relay identity (`relay.role` + `relay.deny_actions`), with
the task's `agent` (informational), `exec_gated` and `bound_files` (both narrowing-only).

### 5.5 Node side
One poll thread, a pool of `max_parallel` workers, a task-id LRU (4096, duplicates ignored), tasks with
`timeout_s <= 0` dropped, `<data>/relay.disabled` stops polling, backoff 1, 2, 4 … 60 s ±20 % on network errors,
60 s after a 409 on register, 300 s after 401/403. Poll HTTP timeout = `poll_wait + 15`. `relay.proxy`: `auto`
(node `network` rules; loopback/private/tailnet hubs direct), `off`, or an explicit `http://` proxy used for every
hub address (CONNECT; TLS and the pin are verified inside the tunnel).

## 6. Model gateway (§8.7)

`/openai/<node>/<service>/v1/{models,chat/completions,completions,embeddings}` (the `v1/` segment is optional) with
a `models` token (`"models": ["desk-wsl/qwen", "pod-a/*"]` on the hub, service globs on a node). The globs are matched
against `<canonical node name>/<service>`. Errors are OpenAI-shaped `{"error":{"message","type","code"}}`
(401 authentication, 403 not in the globs or not a gateway token, 404 unknown node/service — a `models` token is told
only names its globs match —, 413, 429 inflight > 4 per token, 503 offline, 504 timeout).

- Direct links pass the bytes through (SSE streams chunk by chunk); the node receives the hub token with
  `X-Hive-Agent: gw/<models token name>`; the client's `Authorization` is never forwarded.
- Relay links use the action `model.forward` (`{"service","path","body","timeout"}`, non-streaming). A request with
  `stream:true` is sent with `stream:false` and without `stream_options`; a 2xx reply comes back as
  `text/event-stream`: one `data:` chunk per upstream choice (`chat.completion.chunk` with the message as `delta`,
  tool calls with their `index`, `reasoning_content` kept; `text_completion` for completions), a final usage chunk
  with `choices: []` when `stream_options.include_usage` was set, then `data: [DONE]`. A reply larger than the relay
  limit → 502 `relay_truncated`. Non-2xx upstream replies pass through as JSON.
- Reply headers (audit round 3): `Content-Type` only `application/json`, `text/event-stream` or `text/plain`, with
  at most `; charset=<token>`; any other value a pod (`model.forward` `content_type`), a direct node or a model
  server chooses → `application/json`. `Retry-After` passes only as digits; no other upstream header is passed.
  Every server drops response headers whose name is not a token or whose value has a control character
  (< 0x20 except tab, 0x7f).
- On a node: `endpoint_for(service)` (managed service, else a `model_endpoints` entry), vetted with
  `check_url(purpose="model")`; non-2xx and non-SSE bodies are secret-masked; idle timeout 300 s, total 1800 s,
  replies ≤ 64 MiB.
- CORS (default on): preflight `OPTIONS` → 204, `Access-Control-Allow-Origin: *`, echoed request headers,
  `GET, POST, OPTIONS`, max-age 600, never credentials.
- Audit kind `gateway`: token, node, service, path, model, stream, status, bytes in/out, usage — never prompts or
  replies.

## 7. Distribution and join codes (§20.2–§20.4)

- `GET /v1/dist/hive.pyz`: a deterministic zipapp of the hub's package, `X-Hive-Sha256` = sha256 of the body.
- Join code: `hvj1.` + base64url(JSON `{"u": hub_url, "t": relay_token, "p": tls_pin|null, "n": node_name}`).
- `hive _update` (pods with `relay.auto_update`): GET the pyz over the relay transport (same proxy, TLS pin/CA; no
  token), replace `hive.pyz` only when the body's sha256 equals `X-Hive-Sha256` and differs from the running file;
  a 429 is retried up to 5 times honouring `Retry-After` (≤ 60 s).
