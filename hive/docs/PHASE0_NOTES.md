# Phase 0 notes (lead contract notes)

Phase 0 of design v8 is implemented: `errors.py`, `util.py`, `schema.py`, `catalog.py` (38 specs),
`actions/__init__.py`, all 54 modules of the §3 layout (real code where §22.1 says "real", stubs elsewhere),
`tests/helpers.py` with the §21.1 fakes, and the P0 tests. Test command (package root):
`python -m unittest discover -s tests -t .` (the §21 form `python -m unittest discover -s hive/tests -t hive`
from the repository root works too).

Below is every place where the design had to be interpreted. Items marked **CONTRACT** may need a decision
by the lead or a line in DESIGN.md.

## Catalog (§4.3, §9)

1. **CONTRACT: result schemas.** Every `spec.result` is closed at the top level and has the optional
   `masked: boolean`, as v8 requires. Results cannot pass the §4.4 lint, because `node.info` and others nest objects,
   so `lint_schema` is applied to **param** schemas only (catalog and tool params). Inside results:
   - fields that may be `null` or have several shapes carry no `type`, only a description (the validator accepts
     any value for an untyped schema);
   - keys that hold data are open maps `{"type":"object","additionalProperties":true}`. These are the **only**
     exceptions to the §22.3 rule 11 grep gate "every schema additionalProperties:false", and `test_catalog`
     pins the list: `net.http` `headers`, `system.info` `software` and `which.found`, the service `definition`
     (service.define/start/stop/restart/status) and NodeSummary `status` (hub.nodes);
   - nested objects with documented keys are closed, including the fixed-shape `system.info` sections `os`
     (and its `cloud`), `cpu`, `memory`, `disks[]`, `gpus[]` (and `processes[]`) and `network` with the §9.2 keys
     (nullable fields untyped).
   `required` lists only the keys the doc shows as unconditional. The result schemas are drafts until M1 (§22.1).
2. **"No `..`" in service names and revisions** is written as a lookahead pattern:
   `^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,63}$`, and the same for `model.pull revision` (`..` and `//`). The pattern
   is valid in both ECMA-262 and Python. A later strict export might not accept lookaheads; the check would then
   move to the validator.
3. These constraints are not given as numbers in §9. They are conservative additions, each consistent with
   another part of the design:
   - `proc.kill pid` has minimum 1, and the items of `proc.list pids` have minimum 0.
   - The items of `net.ports ports` are limited to 1..65535.
   - `model.chat port` is limited to 1..65535, like `model.endpoints port`.
   - `model.chat.service` and `model.endpoints.service` use the service-name pattern, because they name a service
     or a `model_endpoints` name, which §14.1 says follows that pattern.
   - `net.http auth_env` and `net.download auth_env` use the secret-name pattern from §5.13.
   - `service.define health_expect` has minItems and maxItems 2 (an inclusive range).
   - `net.download headers` has maxItems 50, the same as `net.http`.
4. `service.define` takes `shell`, `cwd`, `env`, `secret_env`, `login` and `gpus` from shell.run, as §9.6 lists. It
   does not take `env_unset` or `stdin`.
5. `system.info sections` has its schema default. The rule "`which` given and `sections` omitted → only `which`"
   must therefore use `ctx.given` in the handler (WP4).
6. `node.info.secrets` items allow the optional `set_at`, because `SecretStore.names()` returns it, while the
   §9.2 example leaves it out.
7. `catalog.get()` for a removed v1 name returns `not_found` with `hint` = the replacement text from `REMOVED`
   and `details {"removed": true, "suggestions": []}`. For an unknown name it returns `details.suggestions`
   (difflib, cutoff 0.6) and a "did you mean" hint. `public_dict.classes_required` is a sorted list.
8. `catalog.deadline` treats these cases as follows:
   - A numeric string (`"60"`) counts as a number.
   - A boolean, NaN, infinity or non-numeric value counts as the schema default.
   - An absent `timeout` with no default counts as 60.
   - Any other absent deadline param with no default counts as 0.
9. Extra module constants: `SCOPES`, `PLATFORMS`, and the pattern constants (`JOB_ID_PATTERN`,
   `SERVICE_NAME_PATTERN`, …), which the tests and tools can reuse.

## schema.py (§4.4)

10. Coercion rule 1 ("null = absent") applies to typed properties. An untyped property (result documentation)
    keeps an explicit `null`. A required typed property given as `null` is reported once, as missing.
11. Rule 3 also wraps a single object into an array whose items are objects, e.g. a lone message
    `{"role","content"}` given for `model.chat messages`.
12. The §4.4 example "unknown parameter 'working_dir' (did you mean 'cwd'?)" cannot come from difflib alone. A
    small alias table (`working_dir`/`workdir` → `cwd`, `cmd` → `command`, `args` → `argv`, `file` → `path`, …)
    is tried first, then difflib with cutoff 0.6.
13. Export restates the `x-*` keywords at the end of the description: `(exactly one of: a, b)`,
    `(at least one of: …)`, `(list of 'KEY=VALUE' strings)` and `(list of 'Name: value' strings)`. The same
    reshaped schema is used for all four formats; the tool layer adds the per-format envelope (§17.7).
14. Beyond §4.4, `lint_schema` also rejects:
    - unknown keywords (e.g. `format`), invalid patterns and defaults that do not validate;
    - arrays of arrays and objects nested deeper than array items;
    - `required` or `x-*-required` entries that name properties which do not exist.
14a. `pattern` uses ECMA-262 end-of-input semantics: `schema.ecma_regex()` (lru-cached) rewrites every unescaped `$`
    outside a character class to `\Z`, because Python's `$` also matches before a trailing `"\n"`. So
    `{"name": "foo\n"}` fails the service-name pattern and `"j_abcdef\n"` the job-id pattern. Code that matches
    catalog patterns outside the validator should use `schema.ecma_regex(p)` (or `re.fullmatch`), not `re.search`.
15. Range errors name the range: "must be between 1 and 100 (got 500)". `bad_request.hint` lists the valid
    top-level parameters.

## errors.py (§4.1)

16. The `HiveError` constructor itself records `details["orig_code"]` when it maps an unknown code to `internal`;
    `from_dict` relies on this.
17. `rate_limited` always includes `details.reason`, which may be `None`.
18. `approval_required` uses the message "this call needs the owner's approval (<where>)" and `hint` = `how`.

## util.py (§4.2)

19. `valid_name` is lowercase only, exactly the §4.2 regex `^[a-z0-9][a-z0-9_.-]{0,maxlen-1}$` (names are canonical
    lowercase, §8.7 "canonical lowercase node name"). The "case-insensitive compare elsewhere" remark is about
    comparison, not validity: callers that accept user input (CLI `--name`, `join`) lower-case it first.
20. `is_local_name` returns False for IP literals: they are not names, so `"::1"` is not a "name without a dot".
21. `ip_category` raises `bad_request` for non-IP input. It also unwraps 6to4 addresses (as listed), strips IPv6
    zone ids and brackets, and checks the metadata addresses both before and after unwrapping.
22. `parse_size`:
    - `k`/`m`/`g` and `KiB`/`MiB`/… are binary;
    - `KB`/`MB`/`GB` are decimal;
    - a bare number is bytes.
23. `parse_iso` reads a naive or date-only value as UTC; token `expires` values are date-only (`2027-01-01`).
    Out-of-range fields are rejected with `bad_request` (hour ≤ 23, minute ≤ 59, second ≤ 60, offset ≤ 14:00 with
    minutes ≤ 59), instead of being normalised into the next day by `calendar.timegm`.
24. `truncate_text` keeps ≤ 48 characters for its marker `…[N chars omitted]…`, so the result stays close to
    `max_chars`. Head and tail are cut at line boundaries when the text has a newline in range.
25. `pid_alive`:
    - a zombie or exited process counts as dead: Linux reads `/proc/<pid>/stat`; macOS/BSD use psutil `status()`
      when installed, else `ps -o stat= -p PID` with `LC_ALL=C` (state `Z…`); Windows uses `GetExitCodeProcess`.
      On macOS this costs one `ps` per call without psutil (like `process_create_time`);
    - when `create_time` is given but the process's ctime cannot be read, it returns alive, because the PID reuse
      cannot be disproved.
26. `boot_key`:
    - On macOS the key is the plain `kern.boottime` seconds string.
    - On Linux, an unreadable boot_id falls back to `"t:"`, and an unreadable pid-1 ctime gives `"<boot_id>:?"`.
    - On other OSes the key is `"t:"`, using `/proc/uptime` or the monotonic clock.
27. `FileLock` behaves the same on both OSes in these respects: the holder JSON is cleared on release, and
    `holder()` returns `None` unless the recorded pid is alive (ctime-checked). On POSIX it truncates and rewrites
    the JSON. On Windows it pads the JSON area to 1024 bytes, because the lock byte sits at 4096.
28. `self_command` uses `sys.executable`; §10.1 spawners use `console_python()` on Windows themselves.
    The helper seams `_zipapp_path`, `_package_dir` and `_site_dirs` exist so tests can patch them.
29. `escape_terminal` also escapes DEL (0x7f). The `secure_file`/`secure_mkdir` Windows path uses the private
    seams `_current_user_sid`, `_icacls_argv` and `_run_icacls`; the `*<SID>` argv test uses them.
30. `replace_with_retry` retries only on Windows. A POSIX `PermissionError` cannot be cured by waiting.
30a. `read_json` reads with `utf-8-sig`, so a config saved with a UTF-8 BOM (Notepad "UTF-8 with BOM", PowerShell 5
    `Out-File`/`Set-Content -Encoding UTF8`) loads; plain UTF-8 is unchanged.
30b. **CONTRACT (WP6): `python -m hive` sys.path order (D56).** `prune_cwd_path()` as "first statement of
    `cli.main()`" is too late for `python -m hive`: `hive.__main__` must import `hive.cli`/`hive._shim` first, and
    their `hive.util` import loads ipaddress, subprocess, json, … from a writable cwd (sys.path[0] for `-m` on 3.9 and
    3.10, and on 3.11+ without `-P`). Therefore:
    - `hive/__main__.py` prunes `sys.path` as the first statement of `_main()` with only `os`/`sys` (same rule as
      `util.prune_cwd_path`: drop `""`, `"."` and the cwd's realpath unless the hive package's parent dir is the
      cwd), before any `from hive import …`;
    - `hive/__init__.py` and `hive/__main__.py` import nothing else at module level, not even `__future__` (a real
      runtime import) — `test_future_annotations_everywhere` exempts those two files; WP6 must keep it that way;
    - `util.prune_cwd_path()` stays first in `cli.main`, `_shim.main` and `_download.main` for the console-script
      path;
    - a subprocess test plants `ipaddress.py`, `json.py`, `__future__.py`, … in a temp cwd and runs
      `python -m hive --version`.
    Residual (interpreter, not Hive): on 3.9 `runpy` itself imports `typing`/`importlib.util` from sys.path before
    any Hive code runs; only `-P` (3.11+) and launchers that start in the data dir (D56) cover that.

## actions/__init__.py (§4.5)

31. `@handler` raises `AssertionError` explicitly, so the check survives `python -O`.
32. `ActionContext.check()` raises a timeout with `maybe_executed = spec.mutating`, the same rule as §13.2 step 9.
33. The `progress` default is a module function, not a lambda. The field order follows §4.5 exactly:
    `…, via, hold, given, progress, audit_extra`.

## Stubs (§22.1) and other modules

34. **CONTRACT: §3 and §22.1 conflict.** §22.1 says to stub *only* the names imported across WP boundaries, while
    §3 and the task require all 54 modules. Every module therefore exists, with the public names of
    §4.6/§5–§20. Names not listed in §22.1 raise `unsupported("<module>.<name> not available yet (WPn)")`,
    except the ones on the degrade paths below (34a).
34a. **CONTRACT (WP1): stubs import cleanly, so `except ImportError` never fires.** The design's fallbacks
    (§13.1 managers late-imported → `_Unavailable` on ImportError; §9.2 node.info lazy null-fallback imports) would
    never trigger, so the names on those paths are **neutral** in Phase 0:
    - `jobs.JobManager` / `services.ServiceManager`: `start()`/`shutdown()` are no-ops, `managed_pids()` → `{}`,
      `ServiceManager.endpoints()` → `[]` (`policy_view()` was already the empty view); everything else stays
      `unsupported`;
    - node.info probes: `proctree.default_shell()` → `None` (the null fallback, although the WP2 signature is
      `-> str`), `proctree.available_shells()` → `[]`, `sysinfo.capabilities()` → `{}`, `sysinfo.summary_line()` →
      `""`, `sysinfo.live_status()` → `{}` (node `GET /v1/nodes`), `gpu.detect()` → `{"backend": None, "gpus": [],
      "processes": [], "errors": ["gpu module not available"], "driver": None, "cuda": None}` (the §12.2 key set).
    WP1 should still treat `unsupported` like `ImportError` (catch `Exception`, fall back to null/`_Unavailable`) in
    node.info and in the manager late-import step, so a partly merged WP cannot crash `NodeRuntime.start()`.
34b. Class hierarchy as declared: `gateway.HubGatewayBackend(GatewayBackend)`, `NodeGatewayBackend(GatewayBackend)`,
    `client.HttpTarget(Target)`, `client.HubTarget(Target)`, `tools.RuntimeTarget(client.Target)` (tools imports
    client at module level; client imports tools only under TYPE_CHECKING, so there is no cycle). Both Protocols are
    `@runtime_checkable`. `isinstance(x, Target)` works; `issubclass(cls, Target)` raises TypeError in Python because
    `Target` has a data member (`kind`) — use `Target in cls.__mro__` in tests. `issubclass(..., GatewayBackend)`
    works.
35. Behaviour chosen for stubs whose value §22.1 does not pin:
    - `policy.Limits` is real, and its fields default to the §14.1 standard values (gateway values from the
      §14.1 `gateway` block), so `Limits()` works for `FakeEffectivePolicy`.
    - The `secretstore.SecretStore` stub behaves as an **empty store**: `names() == []`, mask = identity, and
      `resolve`/`resolve_auth` raise `not_found` with the §5.13 hint. `set`/`unset` are unsupported.
    - `netutil.open_stream` (P0) accepts only `http://` loopback targets. It calls `guard.check_url(...)` when the
      guard has one, and tolerates `unsupported` from the NetGuard stub. Errors follow §11.2: connect phase
      (nothing sent) → `unavailable` reason `connect` (`dns` for a resolver error) or `timeout`, both with
      `maybe_executed=False`; after the request was sent → `timeout(maybe_executed=True)` for a timeout (also while
      streaming chunks) and `unavailable(reason="connect", maybe_executed=True)` for a reset/other socket error.
    - `tools.register_agent_routes` and `gateway.register_gateway_routes` are **no-ops that log a warning**, not
      errors, so WP1's `run_node` and WP5's `run_hub` work before WP5/WP6 merge. `register_agent_routes(...,
      role="node", mcp=True)` raises `ValueError` (§17.1).
    - `procinfo.list_processes` returns `([], "unavailable")`, like `listeners`.
    - `fsops.PartTracker` is a no-op and `prune()` returns 0.
    - `dist.self_update` returns `False` without a request. `python -m hive _update` calls it and exits 0.
    - `hub.HUB_HANDLERS` is `{}` until WP5.
    - `presets.PRESETS` is `{}` until WP2.
    - `_shim.main` and `_download.main` return 70.
    - `cli.main` answers `--version` and otherwise exits 2.
36. The `presets.approval_review` P0 stub uses the pinned shape `{"action","lines","changes_what_runs","files":[]}`,
    plus `note` for a hub-side `service.define`.
    - Line format: `changes what runs: …` first, then `note: …`, `action: <name>`, `env K=V` (values shown), and
      `stdin:` followed by its lines. Other multi-line strings are split into lines, and other params print as
      `key: <json>`.
    - Secret-looking env keys (`SECRET_KEY_RE`) have their values passed through `audit.redact_text`, or `***`
      while audit is a stub.
    - All lines pass through `escape_terminal`. WP2 replaces this with file binding.
37. The `config.py` dataclasses are empty placeholders; WP1 adds the §14 fields. The `PresetCtx` and `Policy`
    signatures that reference them only use them as type names.
38. **CONTRACT: names the design does not give.**
    - `guides.topic_text(topic)` is a stub; §17 names the 10 topics but no function, and the name is a proposal.
    - `RenderCtx` lives in `render.py` and is re-exported by `tools.py`; `CallCtx` lives in `tools.py`. §4.6
      groups both modules together.
39. Extra constants, for readability:
    - `auth.KINDS`;
    - `node.EXIT_STOP = 0` and `node.EXIT_RESTART = 75`;
    - `approvals.APPROVAL_ID_PATTERN` and `approvals.GRANT_ID_PATTERN`;
    - `install.METHODS`;
    - `netproxy.ALWAYS_NO_PROXY`;
    - `presets.RISKY_ENV_PATTERNS` and `presets.HUB_DEFINE_NOTE`;
    - `secretstore.OTHER_SECRET_HOSTS`;
    - `winapi.SPAWN_MODES`.
40. The fail-safe `proctree.trusted_path()` takes PATH, drops relative entries and anything under home, cwd or
    tempdir, and then adds the §12.2 fixed locations: `/usr/lib/wsl/lib` and `/usr/bin`, or
    `C:\Windows\System32` and the NVSMI folder on Windows. Reading of the §10.1 fail-safe: an exclusion base that
    is a filesystem root (`os.path.dirname(p) == p`: cwd `/` as the WORKDIR of many containers, `C:\`, `HOME=/`)
    is skipped, otherwise every PATH entry would be excluded and probes could not find nvidia-smi/docker under
    `/usr/local/bin` or `/opt`.
41. `Policy.net_guard` is described as an attribute in §5.4. The stub declares it as a property, which works
    either way.

## tests/helpers.py (§21.1)

42. The `FakeNetGuard` signature is `FakeNetGuard(resolve=None, **netguard_kwargs)`. Non-http(s) schemes and URL
    userinfo raise `bad_request`. `VettedTarget.path` includes the query string.
43. `FakeEffectivePolicy` is constructed as `FakeEffectivePolicy(identity=None, *, workdir, data_dir, limits,
    resolve)`.
    - Its hard denies are the `HIVE_HOME` tree, the `data_dir` tree and any `.hive` path component.
    - `check_url` resolves names through `resolve`, then the default public IP `93.184.216.34`, then
      `localhost` → 127.0.0.1. It allows loopback and public only (`ssrf` otherwise) and records `url_calls`.
    - `check_timeout` has the real §5.4 semantics.
44. `FakeRuntime` is constructed as `FakeRuntime(base=None, name, node_id, workdir)`. Its `secrets` is a
    `FakeSecrets` (empty, but `set()` works), so `set_secret()` and masking tests run without WP1. `policy` and
    `holds` are settable, and `cleanup()` removes its temp dirs. An owned temp dir (`hiveft-*`) is also removed by a
    `weakref.finalize` registered in `__init__`, i.e. when the runtime is garbage-collected (a `make_ctx()` context
    going out of scope) or at interpreter exit, so handler tests that never call `cleanup()` leave nothing behind.
    `make_ctx` keeps the binding §21.1 signature (no `testcase=` argument).
45. **CONTRACT: `start_node_server` and `start_hub`** raise `unittest.SkipTest` in Phase 0.
    - §13.1 names no entry point that builds the node's `ApiServer` with its routes apart from `run_node`, which
      blocks.
    - Proposal: WP1 adds `node.build_server(rt) -> ApiServer` (not blocking), or WP1 fills in this helper at its
      merge.
    - `make_runtime` is implemented against `config.load_node_config` and `NodeRuntime(cfg,
      start_managers=…).start()`, and skips while those are stubs.
46. `StubHTTPServer` has this API:
    - `add(method, path, status, body, headers, delay)`;
    - `add_handler(method, path, fn)`;
    - `add_file(path, data)`: a single range `bytes=a-b`, `bytes=a-` or suffix `bytes=-n` → 206 with
      `Content-Range: bytes s-e/len` (end clamped); a start at or past the end → 416 with `Content-Range: bytes */len`;
      a malformed or multi-range header is ignored (full 200), as a server that ignores Range would do;
    - `add_sse(path, events)`;
    - `openai(models, reply)` serves `/health`, `/v1/models` and `/v1/chat/completions`, with or without
      `stream`;
    - `start`/`stop`, a context manager, and the `url`, `port` and `requests` attributes.
47. In `fake_tool_on_path(name, script)`, `script` is Python code unless it starts with `#!`. On Windows the
    helper writes `name.cmd` plus `name.py`.
48. The integration gate test "every node-scope spec has a handler after `load_all()`" **skips while no WP handler
    is registered**. It fails as soon as some handlers exist but not all.

## Repository

49. `.gitignore` gained `*.pyz`. No `.pyc` files were tracked, so `git rm --cached` was not needed. Nothing was
    committed.

## WP1 hand-off (contract requests and interpretations)

50. **CONTRACT-REQUEST (WP1): `node.build_server(rt) -> ApiServer`** (the note-45 proposal): builds the node
    ApiServer with every §13.3 route, not started. `helpers.start_node_server(**cfg)` now uses it (real runtime +
    server on 127.0.0.1:0; `preset=`, `start_managers=` (default False), `agent_entry=` extra keys; owner token in
    `rt.test_owner_token`, `rt.test_stop()` stops both). `helpers.make_node_config` now writes only
    `{"preset": preset}` as the policy block, so the loader fills the rest from the preset (otherwise
    `make_runtime("readonly")` was not read-only). `test_helpers.test_wp_dependent_helpers_skip` now expects only
    `start_hub` to skip.
51. **CONTRACT-REQUEST (WP1): additive keyword parameters** (all optional, old calls unchanged):
    - `EffectivePolicy.auth_spec(name, *, method="GET")`: rule 5b (`secret_write`) needs the method; **WP3's
      net.http must pass `method=`** (the design gives rule 5b no other home).
    - `Policy(..., own_ports=())`: forwarded to `NetGuard(own_ports=…)` (the node's listen and proxy ports, §5.4
      rule 5a last sentence); NodeRuntime passes them.
    - `config.default_paths(env=os.environ)` and `config.hive_home(env)` (env-only mode reads the given mapping).
52. **CONTRACT-REQUEST (WP1): additive names** other WPs may use: `SecretStore.max_value_bytes` (bytes of the
    longest maskable value; range readers read `max_value_bytes - 1` extra bytes per side for `mask_edges`; WP2/WP3
    need it), `SecretStore.node_name` (set by NodeRuntime for the not_found message), `audit.set_text_mask(fn)` (the
    secret mask `redact_text` applies; NodeRuntime registers `SecretStore.mask`), `auth.system_identity()` (kind
    `system`, all classes: autostart/supervisor), `approvals.file_fingerprint(path)` / `params_hash`,
    `FileHolds.paths(hold_id)` (for job records / service run state), `ApprovalStore.get(id)`,
    `NodeRuntime.effective(identity)`, `NodeRuntime.implemented()`, `NodeRuntime.request_stop(code)`,
    `NodeRuntime.gpu_info()`, `policy.windows_string_checks()`, `httpserver.Headers`, `httpserver.tailscale_info()`.
    `httpserver.Request` gained defaulted fields `proxied`, `local_ip`, `request_id`, `cancel` (set when a streamed
    raw response cannot be written: the gateway uses it to close the upstream).
53. **Hold hand-over convention (WP2 must follow):** `NodeRuntime.call` releases `ctx.hold` when the handler
    returns **unless the handler set `ctx.hold = None`** after handing the id to JobManager/ServiceManager (which then
    release it with `rt.holds.release(id)` when the process exits).
54. **Review `files` entries for `stat` binding (WP2/WP5):** `ApprovalStore.require` takes the record fingerprint of
    an entry from `fingerprint` when present, else: `bind:"sha256"` → `"sha256:"+sha256` (prefix added if missing),
    `bind:"stat"` → `sha256` when it already starts with `stat:`, else `"stat:<size>:<mtime>"` from the entry's
    `size` and `mtime` keys; `bind:"absent"` → null; `bind:"none"` is not stored. The pinned §5.6 entry shape has no
    `mtime`, so WP2/WP5 should add `fingerprint` (or `mtime`) to `stat` entries. `mtime` is the ISO UTC string fs.stat
    returns; `approvals.file_fingerprint()` builds the same form on the node.
55. **CONTRACT-REQUEST (WP1 → P0/WP2 file `presets.py`):** the P0 `approval_review` stub showed a secret-named env
    value through `redact_text(value)`; the real `redact_text` masks values only in context (`KEY=value`, `--token X`,
    known prefixes), so the bare value leaked. `presets._redact_value` now shows `***` when `redact_text` leaves the
    value unchanged. WP2's real review must keep this behaviour (golden test: `API_TOKEN=…` is hidden).
56. **CONTRACT-REQUEST (WP1 → P0 tests):** `test_catalog.TestHandlersGate` now asserts per action module: a module
    that registered some handlers must register all of its actions; modules that registered none (unmerged WPs) make
    the test skip. With only node.info merged the old form failed (36 missing). `test_stubs.test_wp1_stubs` became
    `test_wp1_real`; `test_secretstore_stub_is_empty` passes `env={}` (this CI environment has `HF_TOKEN` set, which
    the real passthrough correctly reports).
57. **init_node topology values** (the signature has no `--insecure`/`--no-listen` parameters): `topology` is
    `None | "tailscale" | "lan" | "lan-insecure" | "pod"`; `"lan-insecure"` = `--lan --insecure`
    (`allow_insecure_remote`); `listen="off"` = `--no-listen`; `listen="HOST:PORT"` / `"tailscale"`. Relay mode is
    implied by `relay_url`/`join`. On an existing config without `force`, a `join`/`relay_url` replaces only `relay`
    (+ `name` from the join code) and any other changeable flag edits only its block (§14.5 v7/v8); the edited config
    carries `cfg._changed` (sorted block names) for the CLI to print. `init_hub` refuses an existing config without
    `force` (conflict). Interactive parts (proxy/mirror probes, printing) stay in the CLI; `init_node` writes an
    `http://` proxy found in the shell environment to `network.proxy` (§14.2) when `proxy` is not given.
58. **Config loading details:** the raw config is merged over the defaults (policy defaults from its preset), so a
    config may omit any block; an explicit `null` keeps the default. Typed nullable keys (`proxy_port`, `data_dir`,
    `tls.cert`, …) are absent from validation when null and come back as the dataclass default `None`. Config
    errors are `bad_request` ("invalid node config <path>: config.listen.hots: unknown property 'hots' (did you mean
    'host'?)"); a world-writable config is `forbidden`. The hub config accepts `data_dir` (same rule as the node).
    `load_client_config()` returns an empty config when client.json does not exist. `NodeConfig.data_dir` holds the
    resolved absolute directory; `cfg._path` / `cfg._env_only` are set on loaded configs.
59. **Policy interpretations:**
    - Lexical step 4 also catches paths outside every root before any `realpath`/`stat`; `~`-relative and `$VAR`
      expansion is done before it; Windows string checks (step 3) run only on Windows (unit-tested through
      `windows_string_checks` on every OS).
    - Root modes: the most specific (longest) matching root decides (`~` rw + `~/photos` ro → writes under photos
      refused).
    - Hard deny_all also covers `/proc/thread-self/**` and `/proc/*/task/*/{environ,mem}` when a root contains
      `/proc`; the `.hive` component test is case-insensitive on every OS.
    - Interpreter entries (sys.executable, stdlib, site dirs, scripts) are hard deny_write wherever they are (outside
      the roots they are unreachable anyway); `sys.base_prefix`/`exec_prefix` trees only when inside an rw root.
    - Relative deny patterns (no leading `/`, `~` or drive) are treated as `**/<pattern>`.
    - `EffectivePolicy.needs_approval` keeps a covering grant's id in `eff.grant_id` (the pipeline audits it as
      `extra.grant`).
    - `check_url` rule 5c also refuses a gated write whose proxy route has no locally vetted IP or a fake-ip, and a
      direct route to a fake-ip address; it returns the target with `pin_ip=True` otherwise.
60. **ApiServer interpretations:** the 10 s header deadline starts at the first byte of a request; an idle
    keep-alive connection is closed after 30 s. A proxied request's identity carries `source_ip = "proxy:<ip>"`
    (so `ApprovalStore.may_decide` never sees it as loopback, even when the rightmost X-Forwarded-For is
    127.0.0.1); `Request.client_ip` keeps the plain client address for throttling and audit. A route with a matching
    path but another method answers 405 (`bad_request`, status 405); OPTIONS preflights of `cors` routes registered
    with auth `none` are answered by the server itself (the route function is not called). Errors in openai style map
    codes to `type` (`authentication_error`, `permission_error`, `not_found_error`, `rate_limit_error`,
    `invalid_request_error`, `server_error`).
61. **NodeRuntime interpretations:** `call(via=…)` cannot widen: a hub/relay identity keeps its own `via`; the
    concurrency gate's waiting count is the NodeSummary `queued`. node.info composes the WP2/WP4 probes through
    `actions/node.py:node_info_result` with null fallbacks (P0 stubs give `{}`/`None`). On Windows a hand-started
    `hive node run` inside a job relaunches once through WMI and returns that node's exit code; `--supervise` runs the
    launcher loop. `run_node` returns 2 for config errors and 1 for startup failures.
62. **WP1 review fixes (interpretations):** an explicit empty list fails closed: token `classes: []` = no classes,
    `policy.allow_actions: []` allows no action (and contains execution), `policy.fs.roots: []` is a startup error
    (`bad_request`). FileHolds also stop gated writes *around* a held file: `resolve_path(write=True, subtree=True)`
    on a folder holding one, and `denied_under(…, write=True, rebase=dst)` when a held file lies below `dst`, refuse
    with `conflict` rule `approved_input` (`FileHolds.held_keys()` added). Allowlist mode refuses an argv[0] with a
    `..` component. An explicit 100.64/10 `listen.host` is verified against `tailscale_info()` at bind time and then
    served like `"tailscale"` (tailnet socket, plaintext allowed, banner `tailscale (WireGuard)`); the
    single-local-100.64/10-address fallback applies only inside WSL when no tailscale CLI can be run. Auth-`none`
    routes answer a request with a body with 413 and close without reading it; a CORS preflight with a body closes
    the connection. `SecretStore.has_values` re-stats secrets.json on every call. Error masking in step 10a sets
    `meta.masked`. The hub's own listen/proxy ports (when a hub config exists on the machine) join the node's
    `own_ports`.
    - CONTRACT-REQUEST (WP1 → WP4): `EffectivePolicy.check_kill(...)` now returns a dict of audit extras instead of
      None: `{"owner_unknown": True}` when `kill_own_user_only` is on and neither a POSIX uid nor an owner name
      (`info["user"]`, compared without a `DOMAIN\` prefix, case-insensitive on Windows) is known; `{}` otherwise.
      The proc.kill handler merges it into `ctx.audit_extra`. When the uid is missing the owner name is compared,
      and on Windows a known owner of another user is refused (`other_user`). Callers that ignore the return value
      keep working (the tests/helpers.py fake still returns None).

## WP2 hand-off (contract requests and interpretations)

63. **CONTRACT-REQUEST (WP2 → P0 tests):** `test_stubs.TestWp2Stubs` became `TestWp2Real` (the neutral values that
    remain off Windows / without a runtime: `spawn_mode()` "free", `relaunch_outside_job` `unsupported` off Windows,
    `endpoint_for` → `not_found`, `panic_offline` on an empty data dir → empty lists). `test_node.TestHttpRoutes`
    no longer pins `implemented == ["node.info"]`: it asserts `node.info` is listed and every listed action has a
    handler (WP2 registers shell/job/service handlers).
64. **CONTRACT-REQUEST (WP2 → WP1 `node.py`):** `NodeRuntime._review` passes `PresetCtx.secret_names` filtered to
    secrets with `exec` allowed (one-line change), so the review's auto-added `HF_TOKEN` matches what `define`
    stores (`ServiceManager._preset_ctx` uses the same filter; §5.13 "the secret exists with exec allowed").
65. **CONTRACT-REQUEST (WP2, additive signatures):**
    - `proctree.ShellPlan` gains `files: Dict[str, bytes] = {}`: the PowerShell `-File` fallback (> 30 000 encoded
      chars) refers to `{run_dir}/command.ps1`; callers pass `plan.files` as `start_job(extra_files=…)`.
    - `proctree.build_argv(argv, node_path, *, cwd=None)`: a relative argv[0] with a separator (`./run.sh`) resolves
      against the command cwd (never the node's own cwd); bare names still use `node_path` only.
    - `winapi.relaunch_outside_job(argv, env, *, data_dir=None)`: `data_dir` defaults to the directory of the
      `--config` file in `argv` (§14 `data_dir: null` rule), else the cwd; it holds `run/relaunch-env.json` and is
      the relaunched process's CurrentDirectory. WP1's call without it keeps working.
    - `JobManager.get(job_id, tail_lines=20)`; extra JobManager helpers `wait_final`, `record`, `run_dir`, `info`,
      `request_stop`, `mark_ephemeral`, `is_final`; module helpers `jobs.prepare_command` (CommandGuard + argv
      building + child env shared by shell.run/job.start), `jobs.read_log` (shared by job.logs/service.logs),
      `jobs.keep_awake_warnings`; `proctree.which`, `shell_path`, `resolve_shell`, `reap_detached`,
      `adopt_popens`; `presets.describe()` (data for `guide('presets')`, WP6), `candidate_words`, `bind_files`,
      `docker_volume`; `ServiceManager.runtime(name)`; `services.check_base_url`.
66. **Interpretations (WP2):**
    - `proctree.run_quick` raises `timeout` (maybe_executed False, tree killed) on timeout and `not_found` for a
      missing program instead of returning a code; output is capped per stream at `max_output`.
    - Shells (`default_shell`, `build_shell_argv`) resolve through `trusted_path()` plus the fixed system shell
      folders (`/bin`, `/usr/bin`, …; Windows `System32`, `WindowsPowerShell\v1.0`, `PowerShell\7`, Git Bash), never
      a folder inside an rw root; `available_shells` probes `--version` only for bash/zsh/pwsh.
    - `exec.shells` is checked against the resolved shell (`auto` resolves first).
    - shell.run output is cleaned with `clean_terminal_text`; the 25 %/75 % cut inserts `…[N bytes omitted]…`;
      a shim note (`[hive] timeout after N s; stopping`, `[hive] stop_command exit N`) is part of the log.
    - shell.run maps job states: lost → `killed`; a child that could not start → `bad_request` "could not start
      the command: …" (job removed). A shell.run that times out waits for exit.json up to timeout + 3 + 8 s.
    - Job record `pid` = child pid (shim pid until ready.json exists). A job whose shim vanished is `lost` and its
      orphaned child (pid + ctime from ready.json) is killed.
    - JobManager / ServiceManager always take over a pending `ctx.hold` (§13.2 step 8a) and release it when the
      process exits; adoption re-holds the stored paths with the system identity's policy
      (`approvals.file_fingerprint` of the current files).
    - Service definition output (`definition`) lists `env` as its key names; `services.json` keeps the values.
    - `ServiceManager.endpoints()` rows already have the `model.endpoints` row shape (`name`, `source:"service"`,
      `api`, `server`, `base_url`, `healthy`, `models: null`) for WP3.
    - `ServiceManager.endpoint_for`: an `api_key_env` that is a key of the service's own `env` wins over a store
      secret of the same name (the value the server was started with).
    - Health checks before WP3 merges: `_http_status` falls back to the P0 loopback `netutil.open_stream` when
      `guarded_request` is `unsupported`, and the tcp check to a loopback connect when `tcp_check` is.
    - Port preflight uses `procinfo.listeners()` (WP4; rows with `port`, `pid`, `name`), else a loopback connect.
    - Docker preset: `capabilities()["docker"]` (WP4) decides; while it is the P0 stub (`{}`), a docker CLI on the
      trusted PATH counts. A docker daemon that is not reachable is not an error for `panic_offline` (nothing to
      stop). A `~` volume host counts as a path. `docker inspect` errors other than "no such object", "permission
      denied" and daemon-unreachable texts fail the start with the message.
    - The vllm/sglang `model` is passed resolved (PathGuard realpath) when local, else expanded (`~`) by form.
    - Review file binding entries for `bind:"stat"` carry `fingerprint` ("stat:<size>:<mtime ISO>") and `mtime`
      (note 54); `sha256` entries carry the bare hex. Header order: "changes what runs", "not bound to their
      content", "note". Path-like words PathGuard refuses are named "(not resolvable here)".
67. **Open items (WP2):**
    - D40 day-1 spike (IsProcessInJob / breakaway flags of the §20.1 task on Win10 22H2 and Win11 24H2, WMI
      `EnvironmentVariables` replace-vs-merge, winlogon ctime as boot key) needs real Windows machines; every mode
      is implemented, the result is not recorded in DESIGN.md yet. Windows-only code paths (Job Objects,
      suspended start, CTRL_BREAK, Toolhelp32, WMI relaunch) are written against the design but only their
      off-Windows guards and the mocked relaunch env-file flow run in this CI; the Windows tests skip here.
    - (done in the WP2 review fixes, see 68) `service.status.resources.gpu_memory_mb` for containers.
    - The wsl.exe refusal hint does not name a `wsl-host:<node>` tagged node (the node does not know the hub's
      node list).
    - The §22.2 WP2 v8 item "the relay-role gateway hint" belongs to the gateway/model.forward path (§8.7, WP3/WP5);
      no WP2 module produces that error.
68. **CONTRACT-REQUEST (WP2 review fixes):**
    - `JobManager.start_job` `{run_dir}` (narrows §10.4 "every argv element"): substituted in every argv element
      only for the internal helper kinds `download`/`pull` (net.download, model.pull: unchanged for WP3); for
      `job`/`shell` jobs and service spawns only in an element that is exactly `{run_dir}/<an extra_files name>`
      (the PowerShell `-File` fallback of `build_shell_argv`). Agent `command`/`argv` text is never rewritten after
      CommandGuard and approval saw it (it was a path into the data dir past the `.hive` command_deny).
    - `ServiceManager.status(name, tail_lines=30, wait=0, cancel=None)`: additive `cancel` event; the
      service.status handler passes `ctx.cancel` (a cancelled wait returns at once and the handler reports
      `cancelled`).
    - Interpretations: a service exit that does not qualify for a restart (restart=no, on-failure with exit 0, or a
      stop that nobody asked to undo) sets desired `stopped` (the supervisor's "stopped + desired running" start is
      for boot/autostart only). A shim gone without exit.json (killed, OOM) -> its recorded child is killed
      (identity-checked) before the restart policy applies, in `_on_exit` and at node start. An adopted record
      that was `starting` stays `starting` with its original start time (rest of `start_period`; runtime.json keeps
      `log_offset` so ready_pattern matching starts at that run's output). `service.start` during `stopping` waits
      for the stop sequence (forcing after stop_timeout + 7 as the supervisor would, bounded by the call) and then
      spawns with the calling identity (still stopping -> `conflict`, the start is dropped). `endpoints()`
      `server`: known preset, else `ollama` for endpoint_kind ollama, else "unknown" (WP3's probe decides).
      `policy_view().code_dirs` resolve cwd with the system identity's `check_cwd` (relative -> workdir, `~`,
      `$HOME`/`%USERPROFILE%`; no cwd -> the workdir) and argv[0] with `build_argv(..., cwd=)` (so `tools/serve.sh`
      counts); the implicit workdir adds no CODE_DIR_WARNING of its own. Container `gpu_memory_mb` (Linux): host
      pids from `docker top <c> -eo pid` (cached 10 s) are matched too. `resources.cpu_pct` with psutil is null on
      the first sample of a pid (Process objects are kept per service between calls). jobs.max_running is checked
      and reserved in one locked step; the reaper no longer marks a job `lost` while start_job is still spawning its
      shim (a record in `starting` without a shim pid).

## WP5a hand-off (transport.py, client.py)

69. **CONTRACT-REQUEST (WP5a → P0 test `tests/test_imports.py`):** the grep gate forbade `CERT_NONE` in every file,
    while §22.3 rule 11 allows it on the pin-verification path of `transport.py`/`relay.py` and in `netutil.py`'s
    explicit `verify_tls=False` branch. The gate now exempts exactly those three files; `test_transport` pins the
    `transport.py` occurrence to the function `_pin_context` (AST line-range check). relay.py (WP5b) should reuse
    `transport._pin_context` / `HttpTransport` rather than build its own context.
70. **Additive names (WP5a):** `transport.normalize_pin(pin) -> hex`, `transport.cert_pin(der) -> "sha256:…"`,
    `transport.plaintext_ok(host, ips)` (rule C1), `transport.resolve_host(host, port, timeout=5)`,
    `HttpTransport.proxy_route() -> (proxy|None, ips|None)`, `HttpTransport.call_socket_timeout(body, timeout=None)
    -> (seconds, mutating)`, attributes `url/scheme/host/port/proxy/tls_pin/has_token`, settable `timeout` (the
    Toolkit raises it to `wait_budget + 30`, §17.4) and `offline_hint`; `HttpTarget(transport | url, token=None, *,
    kind=None, connection=None, **transport_kw)` with `.transport`, `.url`, `.connection`, `actions_info(node)` (the
    whole `/v1/actions` result incl. `implemented`/`catalog_hash`) and `close()`; `Client.config` (the loaded
    `ClientConfig` or None) and `Client.connection`; `NodeHandle.call(action, params, **kw)`;
    `client.new_request_id()`, `client.split_location(spec)`, `client.LOCAL_CONNECTION = "this-pc"`.
71. **Interpretations (WP5a):**
    - Every connection resolves the host once (≤ 5 s, helper thread) and the direct route connects to those vetted
      addresses (no second lookup), so rule C1 and `proxy_for(..., ips)` decide on the same answer. C1: a token goes
      over `http://` only when the host is loopback, a private/CGNAT IP literal, a name whose resolved addresses are
      all loopback/private (fake-ip 198.18/15 does not count; link-local fe80::/10 and 169.254/16 addresses are
      ignored when at least one other address is loopback/private, since mDNS hosts publish them and they cannot
      leave the link — a name resolving only to link-local is still refused), or an unresolvable local name /
      `*.ts.net`; otherwise `policy_denied` rule `plaintext_credentials`, before any connection is made. A failed
      lookup on a direct route is `unavailable` reason `dns` (retried) before the C1 check.
    - Proxy: `"off"` → direct; an explicit URL (syntax `http://[user:pass@]host:port[/]` checked at construction) →
      `netproxy.load_proxy_settings(None, os.environ, explicit=URL)` on every new connection (§11.4 validation and
      the v4 `wsl-host` substitution; the scheme's URL is used, no `proxy_for`/bypass) → every target through it
      (also loopback/private, §14.4); `"auto"` →
      `netproxy.load_proxy_settings({"proxy": HIVE_PROXY or None, "no_proxy": [], "use_system_proxy": true,
      "ca_file": null}, os.environ)` (HIVE_PROXY as a configured source, so bypass rules still apply; never as
      `explicit=`) and `proxy_for(settings, scheme, host, ips)`. A proxied connection always uses
      `netproxy.open_tunnel` (CONNECT), also for `http://` targets, because Hive requests carry a token (C1); TLS and
      the pin are checked inside the tunnel (C3). Until WP3b replaces the netproxy stub, `auto` is direct and an
      explicit proxy URL fails with the stub's `unsupported`.
    - Connect-phase failures (`connect`, `dns`, `proxy`, TLS handshake incl. certificate verification → reason
      `connect`) are retried ≤ 2 times (0.2 s, 0.4 s); `tls_pin`, `proxy_auth` and C1 refusals never. After the
      first request byte: `unavailable` reason `lost` (no retry; GET/HEAD reconnect once) and socket timeouts →
      `timeout`, `maybe_executed` true only for `POST /v1/call` of a mutating action — an action the client's catalog
      does not know counts as mutating (fail safe) and gets deadline 30. Idle keep-alive connections are not reused
      after 20 s (servers close at 30 s) or when the peer already closed them (select check), so a POST is never sent
      on a stale socket. A non-JSON or non-envelope response is `internal` (details `status`, escaped `snippet`); a
      response `X-Hive-Api` other than 1 is `bad_request` "api mismatch"; responses > 128 MiB are refused (from
      `Content-Length` before reading; chunked/unknown-length bodies are read in 1 MiB steps — never
      `read(128 MiB)`, which preallocates on Python < 3.12). Base URL paths and request paths must be printable
      ASCII without spaces (`bad_request` before any connection).
    - The socket timeout of `POST /v1/call` is `max(timeout or constructor timeout, catalog.deadline(...) + 25)`
      (§15.1; the constructor timeout is the minimum); other routes use `request(timeout=)` or the constructor value.
    - `HttpTarget.kind` is a plain attribute (`kind=` or `"unknown"`), updated by `whoami()`; it never does I/O
      (Python 3.9-3.11 runtime Protocol checks call `hasattr`). `HttpTarget.resolve_kind()` (additive) asks
      `GET /v1/whoami` on purpose while unknown; a failed probe returns `"unknown"` and is not repeated for 30 s.
    - `connect()` sends nothing. Resolution (§14.4): `connect(None)` → `HIVE_URL` (token `HIVE_TOKEN`, else
      `HIVE_TOKEN_FILE`; pin `HIVE_TLS_PIN`) → `HIVE_CONNECTION` → `default`; `connect("name")` / `"local"` (=
      `this-pc`, the agent connection; offline → the D45 hint "start it: hive node run (or hive service install
      node)") ignore `HIVE_URL`; with `HIVE_URL` set, `connect(None)`/`from_config()` load client.json tolerantly
      (unreadable → `config=None`); `connect("http(s)://…")` uses the given token, else `HIVE_TOKEN`/
      `HIVE_TOKEN_FILE` only when `HIVE_URL` is unset or equals the URL (the env credential stays bound to its host,
      §5.1/C2; another URL gets no token), and `HIVE_TLS_PIN` only when the URL equals `HIVE_URL`; client.json connections are not matched by URL (that
      could silently pick an owner token such as `hub-local`). Default node: `connect(default_node=)` →
      `HIVE_NODE` → the connection's `default_node`. Relative `token_file` paths resolve against client.json's
      folder; only the first line of a token file is used. `connect` options: `ca_file`, `tls_pin`, `timeout`,
      `proxy`, `default_node`, `config` (client.json path); unknown options → `bad_request`.
    - `NodeHandle.sh` raises `internal` (details `exit_code`, `stderr`, `state`, `job_id`) unless the state is
      `exited` with code 0. `follow_logs` starts with the tail, then follows `next_offset`; a job's stream ends once
      the job is final and a read returns no text; a service's never ends. `JobHandle.wait(timeout)` returns the
      last status when the time is up. `pull_model(dry_run=True)` returns the plan dict.
    - Transfers: `Client.transfer("local:P" | "NODE:P", …)` (a prefix is required, "self" allowed; a one-letter
      prefix followed by `\` or `/`, e.g. `C:\data\f.bin`, is a Windows drive path → `bad_request`, hint
      `local:` prefix) and
      `NodeHandle.upload/download` call `transfer.copy` with `transfer.LocalFS(None)` and
      `transfer.NodeFS(target, node)` — WP5b must give `NodeFS` that constructor (`node` may be None = the direct
      node / default). Until WP5b they raise the stub's `unsupported`. `HubTarget` stays a WP5b stub.
72. **Open items (WP5a):** `docs/PROTOCOL.md` (WP5) is left for WP5b with the relay protocol. The proxy routes are
    tested with patched `netproxy` functions and an in-test CONNECT proxy; they must be re-run against the real
    netproxy when WP3b merges.

## WP3a hand-off (contract requests and interpretations)

73. **CONTRACT-REQUEST (WP3a → P0 tests):** `test_catalog.TestHandlersGate` reads an optional module constant
    `PENDING_ACTIONS` from each action module: `actions/model.py` is merged in two parts (§22.1: model.endpoints /
    model.chat in WP3a, model.pull / model.forward in WP3b) and lists `("model.pull", "model.forward")` there. Pending
    actions count as unmerged (the test skips naming them) and must not be registered while listed. WP3b removes the
    constant. `test_stubs.test_netguard_stub` became `test_netguard_real` (NetGuard/guarded_request/tcp_check are real;
    `open_download` stays a stub until WP3b) and `test_part_tracker_noop` became `test_part_tracker_real` (the real
    PartTracker writes its JSON file, so the test uses a temp dir instead of the cwd).
74. **CONTRACT-REQUEST (WP3a → WP2 `services.py`, one line):** the start plan's `eff.check_url(endpoint|health_url,
    purpose="health")` now also tolerates `unavailable` (a DNS failure is not a policy decision; the health check
    reports it). With the real NetGuard an unresolvable endpoint host (e.g. `https://evil.example/v1` in
    `test_services.test_endpoint_for_auth`, no DNS in CI) made `service.define` fail. Policy denials still refuse.
    WP2's `_http_status`/`_tcp_check` now run on the real `guarded_request`/`tcp_check` (their P0 fallbacks stay).
75. **CONTRACT-REQUEST (WP3a, additive signatures):** `fsops.read_lines(…, *, line_numbers=False)` (numbering inside
    the byte budget); `fsops.walk(…, skip_dirs=())` (fs.search skips `.git node_modules …` without descending).
    Extra fsops helpers used by actions/files.py: `os_errors` (the §11.1 OS-error mapping as a context manager),
    `decode_bytes`/`encode_text` (lossless round trip: BOM kept, undecodable bytes kept with surrogateescape),
    `split_lines` (only `\r\n`/`\r`/`\n` break lines), `normalize_newlines`, `snippet`, `entry_allowed`,
    `display_text`, `binary_error`, `json_depth_ok`. `actions/model.py` exports `list_endpoints`/`parse_base_url`
    (WP3b's model.forward / gateway may reuse them).
76. **NetGuard interpretations (WP3a):**
    - Denial rules: `ssrf` (metadata/link-local IPs and names, reserved, literal fake-ip, mixed fake-ip answers),
      `net_allow` (category/IP/CIDR/host glob not in the allow list; hint lists it), `http_deny`, `own_port`.
      DNS failure without a usable proxy → `unavailable` reason `dns`, `maybe_executed:false`.
    - Allow/deny entries: `loopback|private|public`, IPs, CIDRs, host globs, each optionally `HOST:PORT` /
      `[v6]:PORT`; IP entries match the address and its unwrapped IPv4 (mapped/NAT64/6to4/Teredo).
    - `own_ports` deny only `purpose="http"` to loopback targets (the node's own API); health checks may reach them.
    - `VettedTarget.category`: the literal's category; for names the first resolved IP's category, `"fakeip"` when
      every answer is fake-ip (evaluated as public for the allow decision); `"public"` for an unresolvable proxied
      name. `pin_ip` is set by NetGuard only for `strict_dns` (never for a fake-ip answer); rule 5c is WP1's.
    - Name resolution is the module function `netutil._resolve(host, port, timeout)` (getaddrinfo in a daemon
      thread, ≤ 5 s); tests patch it to stub DNS.
77. **guarded_request interpretations (WP3a):** besides §11.2 it rejects agent framing headers (`Host`,
    `Content-Length`, `Transfer-Encoding`, `Connection`, `Upgrade`, `TE`, `Keep-Alive`, `Trailer`, `Proxy-*`) and an
    `Authorization` header together with `auth` (`bad_request`), so net.http (WP3b) gets these rules for free. With
    `auth`, the **first** hop must be a host the secret is bound to (`policy_denied` rule `secret_host`, hint with
    `hive node secret set NAME --hosts +host`) and must not be plain `http://` to a non-loopback/private target
    (`plaintext_credentials`); later hops drop the header silently (C2). `Accept-Encoding: identity` and
    `User-Agent: hive` are sent unless given. 301/302 of a non-GET and 303 become GET without body; 307/308 keep
    both; more than 5 redirects → `unavailable` reason `redirects`. A 407 from an absolute-form proxy →
    `unavailable` reason `proxy_auth`; an unreachable proxy → reason `proxy` with the Clash hint. `https` and
    credentialed `http` through a proxy call `netproxy.open_tunnel` (still the P0 `unsupported` stub until WP3b;
    `tests/fixtures/connect_proxy.simple_open_tunnel` stands in for it in tests). TLS failures → `unavailable` reason
    `tls`.
78. **fs.* interpretations (WP3a):**
    - Symbolic links: fs.stat reports a link (`type:"link"`, `link_target`, size/mtime of the target) and fs.delete /
      fs.move act on the link itself, located through a PathGuard-vetted parent; the target must still pass
      PathGuard (a link to a path outside the roots cannot be stat'ed, moved or deleted: conservative).
      fs.list/fs.search/recursive fs.copy never follow links (listed/copied as links).
    - fs.list `sort:"mtime"` and `"size"` are newest/largest first; `total_capped` is also set when the walk hit the
      call deadline. fs.search `file` values are absolute native paths (fs.list entries stay relative); literal
      patterns are matched on the whole line, regexes on the first 2 KiB (`cut:true`); match text is cut to 500 chars.
    - fs.read `encoding` is `locale` whenever the file is not valid UTF-8 (on POSIX it is then shown with U+FFFD,
      fs.edit round-trips the raw bytes). `long_lines_cut` counts cut lines. `max_bytes`/fs.read_chunk `length`
      above `limits.max_read_bytes` → `bad_request` naming the limit.
    - fs.read_chunk `length` is the number of raw file bytes read (the masked `data` may differ, `masked:true`).
    - fs.move/fs.copy `dst` is the final path (no "into this folder" semantics); an existing folder `dst` is a
      `conflict`; the destination check refuses with rule `contains_denied`. fs.delete/fs.move refuse targets
      that are, or contain, a filesystem root, home, the workdir, an fs root or the data dir (rule
      `protected_path`).
    - New files get mode 0644 (POSIX), part files 0600; overwrite keeps the original mode bits.
79. **model.* interpretations (WP3a):** probe rows have `name: null`; config `model_endpoints` rows on non-loopback
    hosts are listed but not probed (`healthy: null`; read risk reaches only loopback and service endpoints);
    service/config/probe rows are deduplicated by normalized base URL in that order; an unanswered config row is
    `healthy:false, server:"unknown"`. model.chat: an `ollama`-kind endpoint without a `/v1` path gets `/v1`
    appended; automatic target = exactly one `healthy:true` row of the probe listing (a service/config row brings
    its own `api_key_env`); `content` beyond `max_chars` is cut 75 % head / 25 % tail; the reply is read up to
    `limits.max_response_bytes`.
80. **CONTRACT-REQUEST (WP3a review fixes → WP1 `policy.py`, P0 `tests/helpers.py`, additive):**
    - `EffectivePolicy.resolve_path(…, nofollow_last=False)`: with True, step 5 resolves only the parent and keeps
      the last component as given, so steps 6–8 (roots, fs.deny, deny_write, hard denies incl. `$HIVE_HOME` and the
      `.hive` component, FileHolds) judge the path of a symbolic link itself. fs.delete/fs.move use it for a link
      (and then require the link's target to pass the normal check too; either refusal is raised, never a fall back
      to the target), and recursive fs.copy / the fs.move EXDEV path use it to re-check every destination entry
      right before it is created.
    - `EffectivePolicy.path_key(path, *, follow=True) -> str`: PathGuard's comparison key (casefolded on Windows,
      macOS and WSL `/mnt/<drive>` paths; realpath'd with `follow=True`, lexical otherwise). fs.delete/fs.move's
      `protected_path` test and the copy/move-into-itself test compare these keys (a differently cased path to an
      fs root can no longer slip past on macOS). `helpers.FakeEffectivePolicy` gained both (normcase keys).
81. **WP3a review-fix interpretations:**
    - Links (note 78 refined): a link in a denied folder, a link whose own path is denied (`~/.hive` → HIVE_HOME,
      a symlinked `~/.ssh`, a symlinked `~/.bashrc` for a deny_write identity) and a link to a path outside the roots
      are refused for fs.delete/fs.move; `protected_path` compares the link's own (lexical) path, so a symlinked fs
      root, home or workdir cannot be deleted or moved either.
    - Recursive fs.copy (and the fs.move cross-filesystem fallback, which is a guarded copy + delete instead of
      `shutil.move`): `_dest_check` stays the fast pre-check; every destination entry is checked again with
      `resolve_path(target, write=True, nofollow_last=True)` before it is created. A refusal stops the copy with
      `policy_denied` rule `contains_denied` (details `entries`, `denied_rule`, `copied_files`; entries already
      copied stay). A walk cut short by the call deadline is `timeout` (`maybe_executed:true`, details
      `files`/`bytes`, hint "copy incomplete: N files copied; …"), never a partial success; the EXDEV move then
      keeps the source. On Windows a link that cannot be recreated (WinError 1314) is skipped and counted in the
      result's optional `skipped_links`; directory links get `target_is_directory`.
    - `fsops.walk(…, state=None)`: when given a dict, `state["truncated"]` tells whether the deadline cut the walk
      short (fs.list also sets `total_capped` from it).
    - fs.write `mode:"create"` is exclusive: the temp file is published with `os.link` (fails on any existing name,
      dangling links included), falling back to `O_CREAT|O_EXCL|O_NOFOLLOW`; a name created meanwhile → `conflict`.
      fs.write_chunk's final replace keeps the replaced file's mode bits (new files 0644).
    - fs.read / fs.read_chunk / fs.edit / fs.search / sha256 open files through `fsops.open_regular(path)`
      (`O_NONBLOCK`; a FIFO, device or socket → `bad_request`, never a blocked thread). `fsops.read_lines(…,
      check=None, mask=None)`: `check` (fs.read passes `ctx.check`) runs per 64 KiB chunk of the streamed path;
      `mask(kept, rest) -> (kept, masked)` (`fsops.EdgeMask`, built on `SecretStore.mask_edges`) hides a secret cut
      by a line cut (LONG_LINE, the caller's `max_bytes`) and sets `masked:true`; fs.search's 500-char cut uses the
      same masker. fs.search `names_only` rows carry `before: []`, `after: []`.
    - Every OS call on a resolved path in actions/files.py goes through `fsops.long_path` (§5.4 step 10).
    - `fsops.check_regex`: verbose mode (an inline flag group containing `x`, or `re.VERBOSE` after compiling) is
      refused; a group holding a quantifier or `|` may repeat at most `MAX_GROUP_REPEAT` = 10 times in all (nested
      bounded counts multiply), so `(a?){22}a{22}` is refused like `(a+)+`. Applies to WP2's `ready_pattern` too.
    - netutil: non-ASCII URL paths/queries are percent-encoded (UTF-8, existing escapes kept), IDN hosts become
      punycode; header names must be RFC 9110 tokens and values Latin-1 (`bad_request` before any connection). Any
      `ssl.SSLError` (not only certificate errors) → `unavailable` reason `tls`. `HttpResult.dns` (additive, default
      `"local"`): `"remote"` when a proxy resolves the name; model.chat and the service/port branch of
      model.endpoints record `route` and `dns` in audit `extra` (§5.4 NetGuard rule 8; WP3b's net.* can reuse it).
    - model.endpoints: managed-service rows are probed with the service's own `api_key_env` AuthSpec
      (`ServiceManager.endpoint_for`), and a 401/403 from a service keeps ServiceManager's `healthy`, so a
      key-protected service can be the automatic model.chat target.
82. **CONTRACT-REQUEST (WP3a review fixes → P0 `catalog.py`, additive):** the fs.copy result schema gained the
    optional integer `skipped_links` (not required; present only when a Windows node without
    SeCreateSymbolicLinkPrivilege / Developer Mode skipped links in a recursive copy instead of aborting halfway).

## WP6a hand-off (tools.py, render.py, guides.py, mcp.py stdio, cli.py init/node/call/sh)

83. **CONTRACT-REQUEST (WP6a → P0 test `tests/test_stubs.py`):** `test_route_registration_noops` no longer expects
    `tools.register_agent_routes` to be a warning no-op: it registers `GET /v1/tools` on a recording fake server (the
    `role="node", mcp=True` → `ValueError` and the gateway no-op checks are unchanged).
84. **CONTRACT-REQUEST (WP6a → WP5b `transfer.py`):** the `fs_transfer` tool constructs
    `transfer.TransferManager(client, local_roots=<Toolkit.local_roots>)` lazily in the tool-host process (§15.3 gives
    no constructor). It calls `start(src, dst, overwrite=bool) -> "tx_…"`, `status(id, wait=s)` (polled in ≤ 1 s
    steps up to the inner wait `w`, which also drives MCP progress notifications ≤ 1/s) and `cancel(id)`. While the
    P0 stub raises `unsupported`, the tool renders that error.
85. **Additive names (WP6a):**
    - tools: `TOOLS` / `TOOLS_BY_NAME` (registry), `TOOL_HIDE`, `DESCRIPTIONS` (§17.5 verbatim), `INSTRUCTIONS`
      (§17.8), `derive_params(tool)` (1:1 params from the live catalog), `parse_options(options)` (service_define
      `options`), `resolve_wait_budget(explicit, *, config, stdio, env)` (§17.4 order), `lint_tools()`,
      `is_mutating(tool)`; `Toolkit.get_tool(name)`, `Toolkit.export(fmt, *, protocol_version=None)`,
      `Toolkit.connected`. `Toolkit(None, …)` is allowed: a static, unfiltered catalog (used by `hive tools export`
      without a connection); its calls answer `unsupported` except `guide`.
    - mcp: `serve_stdio(core, inp, out)` (the stdio loop, testable with byte streams), `build_parser()`,
      `CallHandle`; `McpCore.watch` / `watch_interval` / `start_watcher(send)` / `close(session, wait)` (the hub's
      HTTP handler (WP6b) sets `watch=False`).
    - guides: `LIMITS` (the §17.8 caps, tested), `presets_text()`.
    - render: per-tool renderers named like the tools, `error`, `finalize`, `next_call`, `idle_marker`, `head`,
      `fence`, `table`, `clean`.
    - cli: `build_parser()`, `exit_code_for(err)`, `Out`, `CliError`.
86. **Interpretations (WP6a):**
    - `Toolkit.tools()` returns copies whose `params` are the final input schemas (`node` first, required when §17.3
      says so; `approval_id` when advertised); the registry (`TOOLS`) keeps the §17.5 schemas without them.
    - Every tool forwards only the arguments the caller gave (validated with `defaults=False`, like the v6
      `service_define` rule), so node-side `ctx.given` rules hold for all tools (e.g. `sys_info(which=[…])` without
      `sections` returns only `which`, §9.2). Renderers see the arguments with defaults applied.
    - Filtering: `nodes_list`, `guide` and `list_actions` are never dropped; `fs_transfer` counts as backed by
      `fs.read_chunk`/`fs.write_chunk` (a read-only token keeps it: node → local copies only read); `call_action` by
      every node-scope action. Static mode (server unreachable) filters nothing; a failed `refresh()` keeps the
      previous state.
    - `node` param: static mode (kind unknown) offers an optional `node`; `nodes_list` on a direct target has no
      params. Unknown names → `not_found` with did-you-mean; a direct target accepts `self`/its own name.
    - The 40 000-char lint (§17.2) measures the 28 standard tools' §17.5 schemas (no injected `node`/`approval_id`)
      in compact JSON (`separators=(",",":")`, as the stdio transport writes) in all four formats: mcp 38.2 k,
      anthropic 33.9 k, openai 34.7 k, openai-responses 34.4 k. With an injected optional `node` the static MCP export
      is ≈ 40.4 k; a hub with 12 described nodes adds more (lead: re-measure in Phase 1 as §17.2 says).
    - MCP `title` (top-level) is omitted for 2025-03-26; `annotations` always carry all four hints explicitly (MCP's
      defaults are `destructiveHint:true`/`openWorldHint:true`) plus `title`.
    - Read retry (§17.6): one retry with the same request id after 2 s, cancel-aware.
    - `approval_required`: rendered only from a pattern-checked `details.approval_id` and the Toolkit's own
      `whoami.server` (`hub:<name>` / `node:<name>`); followed by `refresh()`; MCP sends `tools/list_changed` when the
      tool list changed during a call.
    - `hive call` keeps `--json '{…}'` for params (§19); the global machine-output `--json` must come before the
      command (`hive --json call …`). `hive call/sh/node status` default to `this-pc-owner` when no `--connection`,
      `HIVE_URL` or `HIVE_CONNECTION` is set (§14.4). `hive sh NODE`: `self`, `.`, `-`, `local` mean the direct node;
      words after `--` are joined with spaces; stdout/stderr are kept apart (`merge_stderr:false`).
    - `hive node restart` reports the P0 `install.restart` stub's `unsupported` (exit 1) until WP4; config-changing
      commands (`init node` edits, `node token add/revoke`) print "restart to apply: hive node restart" when a daemon
      holds the lock and the restart is unsupported. `node token revoke` refuses to remove the last owner token.
    - `hive init node --config P` is refused unless P is the default path (init writes `$HIVE_HOME`'s config).
87. **Open items (WP6a → WP6b):** hub `/mcp` (`mcp.http_handler` stays `unsupported`; `register_agent_routes(mcp=True)`
    logs a warning and registers only `/v1/tools`), the remaining CLI (`join`, `hub …`, `gateway …`, `approvals …`,
    `service …`, `doctor`, `push`/`pull`, `logs`, `mcp-config`, `tls-pin`, `audit`, `dist`: they answer "not
    available yet (WP6b)" with exit 2), `pyproject.toml` (console script `hive = hive.cli:main`; `_console_command()`
    falls back to `python [-P] -m hive` without it), README, examples and the CI workflow. E2E-M1 runs here as
    `tests/test_e2e_m1.py` (POSIX; the Windows variant is for the lead's CI).

## WP6a review fixes

88. **CONTRACT-REQUEST (WP6a → WP1 `config.py`):** `config.init_node(relay_role=None)` is now the default, meaning
    "not given" (a new config still gets `operator`; `--relay/--join` without a role still applies `operator`). On an
    existing config any given role, **including `operator`**, replaces `relay.role` (§14.5 v8: `--relay-role` on an
    existing config changes that key), so the owner can widen a narrowed pod back without `--force`. Widening does not
    turn `relay.auto_update` back on (narrowing still turns it off). `hive init node` passes `None` when the flag is
    absent (`--relay-role` and `--preset` have no argparse default any more; `--preset` is still "only when creating").
89. **Interpretations (WP6a review fixes):**
    - `list_actions` / `call_action` have `node_param="optional"` (§17.5 `{node?}`): `node` is offered but never
      required. `list_actions` without `node` lists every node's actions (`GET /v1/actions`, the hub's union);
      `call_action` without `node` uses the Toolkit's default node or the only node, else sends none and lets the hub
      resolve it (§8.1); hub-scope actions (`hub.nodes`, §9.10) are always sent without `node`.
    - On a direct node target the Toolkit sends the wire `node` as `"self"` (and `hive sh self|.|-|local` passes
      `"self"`), so a connection's `default_node` (a hub-side name, e.g. from `HIVE_NODE`) never reaches the node's own
      `/v1/call`, which would answer `not_found`.
    - A `refresh()` whose `/v1/nodes` or `/v1/actions` read fails keeps the previous rows / action filter (note 86);
      only complete reads replace them.
    - `render.table` footers: `… N more (<hint>)` names a follow-up that exists for that tool (proc_list "narrow with
      filter or user, or change sort", job_list "narrow with state", list_actions "narrow with node…", …); only
      `fs_list` pages with `offset` and writes its own single footer. Without a hint the footer says "not shown".
    - Node text never starts a line outside a fence: `head()` cleans every part; `generic` cleans keys and fences a
      multi-line value only under a plain key (`^[a-z_][a-z0-9_]{0,40}$`; other keys get JSON); `proc_kill` prints
      integer pids only; numeric progress fields that are not numbers are dropped instead of raising.
    - `service_control` (start/restart) and `service_define` state the §17.4 clamping ("waited 40s (client time
      limit); still starting") like `service_status`; `service_define` renders one `changed: <field>` line per changed
      field (with `= value` for plain scalar fields; command/argv/env stay in the redacted definition lines).
    - model_pull start renders `(0 already complete)` / `(<bytes> already complete)` from `total_bytes −
      to_download_bytes`: the start result has no per-file "already complete" count, so the §17.6 example's
      "(0 already complete)" is expressed in bytes.
    - MCP: tool calls run on a small pool of **daemon** threads (not `ThreadPoolExecutor`, whose workers are joined
      at interpreter exit), `notifications/cancelled` and the EOF cancels send `POST /v1/cancel` from daemon threads,
      so stdin EOF exits within ≈ 2 s even while a hub call hangs (§18.3); `run_stdio` additionally `os._exit`s when a
      non-daemon thread is still alive after the 2 s wait. A message with an `id` but no `method`/`result`/`error`
      gets `-32600`. `HIVE_MCP_WATCH_INTERVAL` (seconds, below 30) shortens the watcher interval; tests only.
    - CLI output: `cli.main` (and `run_stdio` for stderr) reconfigure stdout/stderr with `errors="backslashreplace"`,
      and `Out.foreign` falls back to backslash escapes, so node text outside a pipe's ANSI code page (cp936/cp1252)
      cannot turn `hive sh`'s remote exit code into a traceback.
    - `_console_command()` looks for the console script next to `sys.executable`, in `sysconfig.get_path("scripts")`
      and in `<python dir>/Scripts`, then searches PATH by hand without the current directory or relative entries
      (D56; `shutil.which` searches the cwd first on Windows before Python 3.12).

## M1 integration (lead, §22.1 step 3)

90. **E2E-M1 moved to `tests/integration/test_e2e_m1.py`** (§3: `tests/integration/` is the lead's). The WP6a
    in-process variant `tests/test_e2e_m1.py` (note 87) is removed: the new test drives only real processes —
    `hive init node` (temp `HIVE_HOME`, `--listen 127.0.0.1:<free port> --root <tmp> --proxy off`), a real
    `hive node run` daemon, `hive mcp --local` (the `this-pc` agent connection init wrote) and the CLI (`hive call`,
    `hive sh`, `hive node status|stop`, owner and `--connection this-pc`). Flow: tools/list → shell_run (and a 1 s
    hard timeout) → job_start(argv) / job_status(wait) → fs_write / fs_read / fs_edit / fs_search / fs_op copy+delete /
    fs_list / policy-denied read outside the roots → service_define of a stub OpenAI server through the **llamacpp
    preset** (a `llama-server` sh wrapper; Windows defines it with `options=['argv=[…]']`) → service_status(wait) →
    model_chat (MCP), model.chat (owner CLI), model.endpoints (agent CLI) → `hive sh` exit codes → CLI error exit
    codes (policy_denied 3, not_found 1) → service_control remove → MCP EOF exit 0 → audit rows carry the agent's
    own identity → `hive node stop` exits the daemon with 0. When the CI user is root the test sets `allow_root` in
    the written config (§13.1 refuses root otherwise). The Windows branch is written for the lead's Windows CI and
    is not exercised here.
91. **CONTRACT-REQUEST (M1 → WP2 `actions/shell.py`, behaviour fix):** the Toolkit sends `yield_after = min(timeout,
    w)` (§17.4), so every shell_run with a timeout ≤ the wait budget had `yield_after == timeout`; the handler yielded
    at that instant while the shim was killing the command, and the agent read "still running after 1s → background
    job" for a command that had timed out. Now, with `on_timeout:"kill"`, a `yield_after ≥ timeout` is treated as
    `timeout + TIMEOUT_GRACE + 1` (wait for the shim's timeout result, bounded so the call stays inside the client's
    budget). `yield_after < timeout` and `on_timeout:"background"` are unchanged. Regression tests:
    `tests/integration/test_m1_fixes.py`, plus the E2E's 1 s-timeout shell_run.
92. **CONTRACT-REQUEST (M1 → WP6 `render.py`, cosmetic):** `render._s` spells booleans the JSON way (`true`/`false`)
    so definition lines read `autostart: false` (agents copy these into tool arguments); was `autostart: False`.
93. **(M1 → WP6 test `tests/test_render.py`):** `test_fs_list_paging` was timing-flaky (`mtime = now − 120` truncated
    to ms could render `1m59s ago`); it now uses `now − 120.5`.
94. **M1 observations (no change made):**
    - `service.define` health checks run every `health_interval` (default 15 s) also while `starting` (§10.5), so a
      server that needs 0.3 s to bind can report `healthy` only ~15 s after the define when the first check raced
      its bind. Per design; a shorter first-checks cadence during `start_period` would be a design change (lead).
    - `hive init node --listen 127.0.0.1:0` is accepted and writes client.json URLs with port 0 (useless for the
      CLI/MCP); consider refusing port 0 in `init_node` (tests use free ports instead).
    - Several renders point at commands that are WP6b/WP4/WP5b gaps at M1: approval_required says "hive approvals
      approve ap_…" (CLI answers "not available yet (WP6b)"; the owner HTTP route works), service renders say "hive
      gateway url NODE SERVICE" (WP6b) while the node logs "model gateway routes not registered" (WP5) yet
      `node.info.modes.gateway` is `true`; config-changing CLI commands print "restart to apply: hive node restart"
      but `install.restart` is the WP4 stub (a foreground daemon must be stopped and started by hand); `fs_transfer`
      answers `unsupported` (WP5b TransferManager).
    - Without `pyproject.toml` (WP6b) the MCP snippet printed by `hive init node` (`python3 -P -m hive mcp --local`)
      only works when `hive` is importable (PYTHONPATH / installed).

## WP5b hand-off (hub.py, relay.py, gateway.py, transfer.py, dist.py, client.HubTarget)

(Items numbered B1… so they cannot collide with the notes of WP3b/WP4, which merge in parallel.)

B1. **CONTRACT-REQUEST (WP5b → P0 `tests/helpers.py`):** `start_hub(nodes=(), **cfg)` is real: a `Hub` + its
    `ApiServer` on 127.0.0.1:0 (temp data dir, `network.proxy: off`), tokens `{"owner", "agent"}` (agent
    `test-agent`, operator; `agent_entry=` overrides; `extra_tokens=[(key, entry), …]` adds more, plaintext under
    `key`); `hub.test_stop()`. `test_helpers.test_wp_dependent_helpers_skip` now starts a real hub.
B2. **CONTRACT-REQUEST (WP5b → P0 `tests/test_stubs.py`, WP6 `tests/test_tools.py`):** `test_build_pyz` checks the real
    zipapp (and `self_update(None)` without a node config → False); `test_route_registration_noops` now expects the
    gateway routes (GET/POST/OPTIONS `/openai/{node}/{service}/{rest*}`); `test_fs_transfer`'s last block expects the
    real TransferManager (a transfer status, no `unsupported`).
B3. **Additive names (WP5b):** `hub.build_server(hub) -> ApiServer` (the hub routes, not started; like
    `node.build_server`), `hub.write_hub_code(data_dir)`, `hub.summary_from_info(...)`, `hub.RelayIds`,
    `Hub.resolve(node)`, `Hub.whoami(identity)`, `Hub.approval_set(identity, link)`, `Hub.pyz()`,
    `Hub.request_stop(code)`, `Hub.relay_tokens`, `Hub.limits`; `NodeLink.preset()/os_name()/node_id`;
    `RelayLink.register/poll/result/reap/request_restart`; `DirectLink.probe()`; `relay.NetworkTransport`
    (HttpTransport whose `auto` proxy follows a node/hub `network` block), `relay.relay_transport(cfg, network=,
    token=, timeout=)` (also used by `dist.self_update`), `relay.check_hub_url`, `relay.relay_token`,
    `relay.load_instance_key`; `transport.HttpTransport.open_stream(method, path, body, headers=, timeout=,
    cancel=) -> (status, headers, chunks)` (fresh connection, connect-only retry; the hub's gateway passthrough);
    `gateway.synthesize_sse`, `rewrite_stream`, `normalize_path`, `ROUTE`; `transfer.PART_SUFFIX`,
    `LocalFS.sha256`, `NodeFS.sha256`, `NodeFS(target, node=None)`, `TransferManager(client, *, local_roots=None)`
    (`start(src, dst, overwrite=False, chunk=…)`); `dist.pyz_sha256(data)`, `dist.self_update(relay_cfg=None, *,
    pyz_path=None)` (test seam), `dist.add_node_instructions(hub_cfg, name, token, *, via=None, tailnet_ip=None,
    pyz_sha256_hex=None, tls_pin=None) -> {"variants","codes","lines"}` — the §20.4 T/A/B/C blocks; the WP6b CLI
    (`hive hub add-node --relay [--via]`) prints `lines` (it creates the relay token and the hub entry itself).
B4. **Interpretations (hub):**
    - §8.2 order: a relay link that is offline or `identity_changed` is refused in `RelayLink.ensure_fresh()` (before
      the hub checks), so an offline node answers `unavailable offline` immediately instead of a D29 `not_found` for
      an empty action list. Hub-scope actions ignore `node`; an action the hub does not know → `not_found` "the hub
      runs hive X … upgrade the hub" (removed v1 names keep their replacement hint).
    - The approval set is computed only for `kind:agent` tokens (owner/models: none); `exec_gated` uses it for every
      identity (`auth.exec_gated`), so a `models` token's `model.forward` is sent with `X-Hive-Exec-Gated: 1`
      (narrowing only). With `approval_id` given the hub skips the review/file binding and only consumes.
    - Hub file binding sub-calls (§8.2 step 4) use request ids `<rid[:72]>.b<i>` (i from 1; `.b0` is the
      `service.status` read of service.start/restart). `bad_request` from a sub-call counts like `not_found` (word not
      bound). `fs.read` is shown only when complete (`next_start_line` null, no cut lines, `version` set) and its
      `max_bytes` is the remaining shown budget (≥ 1024); otherwise `fs.stat {sha256:true}` binds with
      `shown:false`. The not-bound header goes right after "changes what runs:" (or first).
    - Cancel: `POST /v1/cancel` on the hub sets the call's event and forwards (`/v1/cancel` on a direct node, a relay
      `cancel` for a delivered task; a still-queued relay task is dropped with `cancelled`).
    - Relay queue: a queued task whose call deadline passes before pickup ends as `timeout` (`maybe_executed:false`);
      the pickup-timeout reason is `busy` when the node polled while the task waited (no free slot, or the
      `model.forward` slot reserve), else `not_polling`. Hub stop fails queued tasks (`maybe_executed:false`) and
      delivered ones (`maybe_executed` = mutating) with `unavailable` reason `lost`.
    - `POST /v1/hub/nodes/{name}/restart` sets the flag only while the node is online (`queued:false` otherwise);
      `/ack` on a direct node → `bad_request`; an ack with a hash other than the current pending one returns
      `bound:false` with `refused` "the pending registration changed; run hive hub ack-node again".
    - The relay token store is separate from the agent store: a relay token on `/v1/call` and an agent token on
      `/v1/relay/*` both get 401 (not 403) — neither is a valid token for that route.
    - `GET /v1/dist/hive.pyz`: the zipapp is built at hub start in a background thread (and on first request);
      6/min per client IP, ≤ 4 concurrent, `dist.enabled:false` → 404.
    - NodeSummary rows: relay rows from register `info` (+ poll `status`, ≤ 4 KiB); direct rows are the node's own
      row with `name`/`mode`/`tags`/`description`/`last_call_at` replaced. `nodes_for` does not probe synchronously
      (a direct row before the first probe has `preset: null` and `online: false`).
B5. **Interpretations (gateway):** an unknown node → 404 whose did-you-mean lists only node names the token's globs
    name; a known node whose `<canonical>/<service>` pair is outside the globs → 403 (§8.7 step 1; the message names
    only what was asked, so T18 holds); a 404 from the node over a direct link, or `not_found` from a relay
    `model.forward`, is replaced by the hub's own 404 listing only the token's matching globs. (The §21.2 v8 test text
    "desk-wsl/other → 404" is read as "no other names leak"; we answer 403 per §8.7 step 1 — lead to confirm.) The
    per-token inflight key is token name + sha256 on the hub and `display_name` + sha256 on a node. Node-side: when the
    ServiceManager is not running, `model_endpoints` names still resolve; `ollama` endpoints get `/v1` appended.
B6. **Interpretations (relay agent):** status is sent on the first poll after register and then every 30 s;
    `relay.disabled` drops the session (re-register when it disappears); a `refresh:"restart"` calls
    `rt.request_stop(75)` and stops the poll loop (run_node then stops the relay and returns 75). Task identity:
    `relay_identity(rt.config)` + `agent` (dropped unless it matches `^[A-Za-z0-9._/-]{1,64}$`) + `exec_gated`; the
    `forbidden` of `model.forward` for a role without `net` gets the §7.5 v8 hint in relay.py.
B7. **Interpretations (transfer):** `copy()` refuses an existing destination without `overwrite` (and a folder
    destination) before any chunk; a part file larger than the source restarts at 0; a `conflict` with
    `details.part_size` during the copy resumes once from that size (source sha first); cancel is checked before
    every read and write (the part file stays for a later resume); a zero-byte file is one final empty chunk.
    `LocalFS(roots)` resolves relative paths against the first root; `TransferManager.start` refuses local paths
    outside the roots synchronously; >4 running → `rate_limited` reason `inflight`; a `"self:"` location is sent as
    node `None`.
B8. **Interpretations (dist):** `build_pyz` packs `hive/**` (`.py`, `.json`, `.txt`, `.md`, `.tmpl`, `.typed`; no
    `__pycache__`) from the source tree, or from the running zipapp, with fixed timestamps and sorted names behind a
    `#!/usr/bin/env python3` line; the root `__main__.py` calls `hive.__main__._main()` (so D56 pruning and the
    `_shim`/`_download`/`_update` dispatch apply). `self_update` logs and returns False when not running from a
    zipapp (pip installs), when the hub answers non-200, or on a sha mismatch.
B9. **Open items (WP5b):** the CLI wiring (`hive hub …`, `hive join`, `hive gateway url`, `restart-node --outdated`
    with its re-registration wait, `ack-node` prompt) is WP6b; `hub add-node` text is provided by
    `dist.add_node_instructions`. `/mcp` on the hub stays WP6b (`register_agent_routes(mcp=…)` is called). The relay
    and gateway paths were tested against the real WP3b `model.forward` handler and netproxy as merged at the time of
    this hand-off; E2E M2 (lead) still has to run them together with the CLI.

## WP3b hand-off (netproxy real, open_stream/open_download, _download, hfhub, net.*, model.pull, model.forward)

(Items numbered W1… so they cannot collide with the notes of WP5b/WP4, which merge in parallel.)

W1. **CONTRACT-REQUEST (WP3b → P0 `tests/test_stubs.py`, `tests/helpers.py`):** `test_netguard_real` now checks the
    real `open_download` (refused connect → `unavailable` `connect`), `test_netproxy_no_proxy_stub` became
    `test_netproxy_real`, and the `open_stream` stub tests pass a `FakeNetGuard` (a guard is required now; the P0
    `guard=None` loopback path is gone). `FakeEffectivePolicy.proxies` returns `load_proxy_settings({"proxy": "off"})`
    (the real loader reads `HTTPS_PROXY`, which CI runners set; the fake stays proxy-free). `actions/model.py` no
    longer has `PENDING_ACTIONS`; `test_files.test_write_read_masked` asserts the WP3b actions are implemented.
W2. **CONTRACT-REQUEST (WP3b → WP1 `policy.py`, additive):** `EffectivePolicy.deny_patterns(*, write=True) ->
    List[str]` (public wrapper of `_active_patterns`: hard denies, fs.deny, hard deny_write and, when gated,
    fs.deny_write, PATH dirs and service code dirs). net.download hands these to the `_download` child for
    server-chosen names (§9.8 "the identity's normalized deny patterns").
W3. **Additive names (WP3b):** `netproxy.parse_registry(enable, server, override)`, `netproxy.clear_cache()`,
    `netproxy.CHILD_NO_PROXY` (the §10.1 step 3 list), a redacting `ProxySettings.__repr__`; `hfhub.clear_cache()`,
    `hfhub.REPO_RE`; `_download.run(spec, stop=None) -> exit code` (in-process runner used by tests),
    `_download.choose_name(headers, url)`; `actions/net.py` helpers `auth_spec(ctx, name, method)`,
    `first_hop_auth(auth, target)`, `start_download_job(ctx, kind=, spec=, name=, tags=)`. Download spec additive
    keys: file mode `casefold` (from `EffectivePolicy.path_key`, so WSL `/mnt/<drive>` keys match the deny patterns);
    pull mode `revision`, `skipped_invalid`, `skipped_pickle`, `skipped_unsafe` (copied into result.json).
W4. **netproxy interpretations:**
    - URLs: `http://[user:pass@]host[:port][/]`, normalised without the slash and with port 80 when absent; anything
      else (socks*, https://, PAC, a path) is ignored with one WARNING per distinct value. An invalid `explicit` URL
      (relay.proxy / connection.proxy) is `bad_request`; an explicit URL has source `config` and serves both schemes.
    - Env: `X_PROXY` before `x_proxy`; `ALL_PROXY` fills a scheme only when that scheme's own variable is unset.
      no_proxy = `network.no_proxy` + env `NO_PROXY` + the Windows `ProxyOverride` entries (system source) +
      `ALWAYS_NO_PROXY`. macOS bypass lists are not read (getproxies has none).
    - bypass(): `<local>` (names without a dot), IE-style globs (`10.*`, `*.corp.com`), and IP/CIDR entries are also
      matched against the caller's resolved `ips`.
    - `wsl-host` outside WSL → `bad_request` at load (the node refuses to start with that config error); inside WSL
      without a default route → one warning and no proxy until the next refresh.
    - open_tunnel peeks the proxy's answer (`MSG_PEEK`) so no tunnelled byte is consumed; a CONNECT that is not
      answered in time → `unavailable` reason `proxy` (with the Clash hint); errors carry `details.proxy` redacted.
    - child_proxy_env: `NO_PROXY` = the fixed §10.1 list + `network.no_proxy` (the node's env NO_PROXY is not
      merged; it is inherited anyway unless the policy scrubs it).
    - **Observation (lead):** `Policy` builds its ProxySettings once per config load (D34), so a running node does not
      pick up system-proxy toggles or a new `wsl-host` address every 60 s; the 60 s cache only helps per-connection
      users (transport, relay) and new loads. WSL NAT normally restarts the node with the VM, so this rarely bites.
W5. **netutil (WP3b part):** `open_stream` attaches the secret only where C2/C4 allow it and otherwise drops it
    silently (no `secret_host` error, unlike guarded_request's first hop: the gateway must not fail on a service whose
    key is bound elsewhere); `connect_timeout` covers connect + TLS handshake, `idle_timeout` every read.
    `open_download` keeps `Range` on every hop, drops user credential headers cross-host and returns the streaming
    iterator of the final hop (closing it closes the connection). WP5a's proxy paths now run on the real netproxy
    (`tests/test_net.py` re-checks an explicit proxy URL, note 72).
W6. **`_download` interpretations:**
    - The child `chdir`s into the folder of its `--spec` file (= the run dir), because `start_job(cwd=…)` is the
      child's cwd and the run dir is unknown before `start_job`; handlers pass the workdir as `cwd`.
    - Exit codes: 0 ok, 1 failed (HTTP/network/conflict), 2 bad spec, 3 hash/size mismatch, 4 disk space, 5 name
      refused, 6 folder lock busy, 7 stopped. A failure's last stdout line is `error: …` (the JobManager's `error`).
      A stop (SIGTERM / CTRL_BREAK / SIGINT) finishes the current writes, flushes the manifest and exits 7.
    - File mode: transient errors (5xx, 408/429, connection) retried 3× with backoff (resume from the part);
      `path` mode re-checks "exists and not overwrite" before replacing. Dir mode streams into a hidden
      `.hive-download-<run>.hive-part` in the folder (deleted on failure); the name is chosen and checked when the
      headers arrive, before the first byte is written; a server-sent name containing `/` or `\` (e.g. `../x`)
      becomes `download.bin` (§21.2 row), a denied name fails with exit 5 even if a file of that name exists, symlinked
      candidates are skipped (`-1`, `-2`… continue).
    - Pull mode: files are fetched smallest first; an existing file that is not in the manifest and differs (same
      size, other hash) fails that file "exists with other content (use overwrite=true)"; a body larger than the
      listed size counts as a hash failure (part deleted, one retry, exit 3); a short body resumes and counts toward
      the 5 network retries; the manifest is written after every file and keeps stale entries; result `files` and
      `skipped_existing` are counts.
W7. **hfhub interpretations (live APIs checked 2026-11):** HF tree entries give `lfs.oid` = sha256 (used) and
    `xetHash` (not a sha256, ignored); non-LFS files are verified by their git blob sha1 (`oid`). HF answers 401
    `RepoNotFound` for missing *and* private repos → `not_found` with the token hint; `GatedRepo` (or a gated/restricted
    body on 401/403) → `forbidden` with the gated hint. ModelScope has no HEAD sha in the listing: the commit is the
    newest file `Revision` (all listed files are unchanged since that commit), and `repo?Revision=<sha>&FilePath=`
    serves files at that sha (checked live). A listing still running at the call deadline continues in a daemon
    thread (≤ 600 s) and fills the 10 min cache, so the retry the timeout hint asks for is fast; the cache key also
    holds the token name. The ModelScope token is sent as `Authorization: Bearer` (not verified with a private repo).
W8. **model.pull interpretations:** dry runs are not counted against `max_concurrent_pulls`; the busy check runs once
    early and again under the module lock right before `start_job`; refused targets use rule `protected_path`;
    `models.dir: null` → `~/models`; the unbound-token note is also the hint of a gated/401 error while anonymous;
    the dry-run `note` additionally names files that would be skipped as unsafe (the closed dry-run schema has no
    `skipped_unsafe`). A target that is non-empty without a manifest of the same source+repo is a `conflict` (entries
    listed) before the per-file size check.
W9. **model.forward / net.* interpretations:** model.forward appends `/v1` to an `ollama`-kind base like model.chat,
    uses `GET` for `models`, and records `service`, `path`, `status`, `bytes_out`, `model`, `stream` (as requested) and
    `usage` in audit `extra`; the reply is decoded as UTF-8 with replacement. net.http returns `body` when the
    content type is textual or the bytes are valid UTF-8 without NUL, else `body_base64`; header values are cut at
    4096 chars; `max_bytes` is capped by `limits.max_response_bytes`. net.download vets the first hop (and the
    secret's host binding / C1) in the handler before starting the job. net.ports reads `procinfo.listeners()`
    (WP4; the P0 stub gives `method: "unavailable"`) and checks ports in parallel threads.
W10. **Open items (WP3b):** Windows/macOS-only paths (winreg read, SIGBREAK in `_download`, no `O_NOFOLLOW`) are
    tested with mocks only; a private ModelScope repo with a token was not tried. The opt-in live check
    `HIVE_LIVE_NET=1 python -m unittest tests.test_pull.TestLiveListing` (skipped by default) lists
    `openai-community/gpt2` on huggingface.co and `Qwen/Qwen2.5-0.5B-Instruct` on www.modelscope.cn through the real
    netproxy (passed here through the env HTTPS proxy, i.e. a real CONNECT tunnel + verified TLS).

## WP4 hand-off (sysinfo, gpu, procinfo, wsl, actions/system, install, doctor, deploy/*)

(Items numbered K1… so they cannot collide with the notes of WP5b/WP3b.)

K1. **CONTRACT-REQUEST (WP4 → P0 `tests/test_stubs.py`):** `TestWp4Wp5Wp6Stubs.test_procinfo` became `test_procinfo_real`
    (`process_info` has the v3 shape), `test_node_info_probes_neutral` became `test_node_info_probes_real` (capability
    keys, `live_status` keys, the `gpu.detect` key set) and `test_install_restart` became `test_install_restart_real` (a
    node that is neither running nor installed → `unavailable` with the "hive node run" hint).
K2. **CONTRACT-REQUEST (WP4 → WP6 `tests/test_cli.py`):** `hive node restart` with no daemon and nothing installed now
    exits 4 (`unavailable`, hint "start it: hive node run (or hive service install node)") instead of the stub's 1.
K3. **CONTRACT-REQUEST (WP4 → WP2 `tests/test_services.py`):** the two daemon-down container tests
    (`test_daemon_down_waits_then_starts`, `test_daemon_down_cap`) patch `hive.sysinfo.capabilities` (docker true) around
    the define: the real probe runs `docker version`, and §10.6 refuses the docker preset while the daemon is down; the
    tests model a daemon that goes down after the define (the §10.5 start-time wait). The other container tests run
    against the real probe (the fake docker's `version` exits 0).
K4. **CONTRACT-REQUEST (WP4 → WP1 `node.py`, one line):** `wsl.Keepalive(self.state_dir)`. Additive signature
    `Keepalive(state_dir=None, *, wsl_exe=None, clock=time.monotonic)` (default state dir: `default_paths()["node_data"]
    / "state"`; `wsl_exe`/`clock` are test seams).
K5. **Additive names (WP4):**
    - `sysinfo`: `network()` (the §9.2 network section), `which(names, *, node_path, versions)`, `tailscale_ip(addrs)`,
      `container_limit(total)`, `version_line(name, path)`, `software(names, refresh=False)`, `capabilities(refresh=False)`,
      parsers `parse_mounts`/`parse_df`/`parse_vm_stat`, `clear_cache()`, `SOFTWARE`, `VERSION_NAMES`.
    - `gpu`: `note(result)` (→ `gpu_note`), `summary(gpus)`, parsers `parse_query_gpu`/`parse_compute_apps`/
      `parse_cuda_version`/`parse_lspci`/`parse_win_video`/`parse_system_profiler`, `clear_cache()`, `WSL_NOTE`,
      `AMD_NOTE`, `NO_SMI_NOTE`.
    - `procinfo`: parsers `parse_proc_stat`/`parse_proc_net_tcp`/`parse_lsof`/`parse_netstat`/`parse_lstart`/
      `parse_ps_rows`/`parse_ps_args`/`split_windows_cmdline`. `list_processes` rows: `{pid, ppid, name, user, uid, cpu_pct,
      mem_bytes, create_time, cmdline}`; `listeners` rows: `{proto, address, port, pid, name}` (what WP2/WP3 read).
    - `wsl`: `interop_enabled()`, `networking_mode()`, `systemd_running()`, `has_distros()` (capabilities `wsl`),
      `version_line()`, `wsl_exe()`, `decode_output`/`parse_list_verbose`/`parse_list_quiet`, `clear_cache()`.
    - `install`: `render_systemd`/`render_plist`/`render_task_xml`/`render_watchdog`, `systemd_quote`, `captured_path`,
      `check_launcher(data_dir)`, `TASK_NAMES`; `status()` returns `{kind, installed, method, system, files, running, pid,
      data_dir, active}`.
    - `doctor`: `parse_icacls`, `parse_powercfg`, `firewall_rule_present`, `ENDPOINTS` (the network targets; tests point
      them at stub servers).
    - `actions/system.protected_pids(ctx)`.
K6. **Interpretations (probes and actions):**
    - `capabilities()` is cached 60 s per trusted PATH (a node or test with another PATH probes again); `docker` = the CLI
      on the trusted PATH and `docker version --format {{.Server.Version}}` exits 0 in 5 s; `docker_gpu` is `false` on
      Linux when Docker is unusable, `null` on Windows/macOS. `nvidia_smi` = `gpu.find_tool("nvidia-smi")` (false on macOS).
    - `system.info`: `refresh` clears the sysinfo/gpu caches; os/cpu static parts cached 60 s, `software` 5 min per
      trusted PATH; `which` looks names up on `ctx.policy.node_path` and runs `--version` only for `VERSION_NAMES` hits whose
      folder is on the trusted PATH (node PATH minus trusted PATH = folders inside rw roots → `note`); `ffmpeg` uses
      `-version`, nvcc reports its "release" line; a found tool whose version probe fails reads "unknown version @ path".
    - GPU rows without a size (lspci/CIM names) have `memory_total_mb: 0` (the result schema types it as a number);
      `gpu_backend` is `nvidia|apple|cim|lspci|null`; `gpu_note` joins `errors` with "; "; an empty process list inside a
      container while memory is in use adds the PID-namespace note; a failing nvidia-smi falls back to lspci/CIM names and
      keeps its first output line in `errors`.
    - `proc.list`: `filter` matches the name or the command line (the command line is read for filtering even with
      `cmdline:false`); `user` compares without a `DOMAIN\` prefix, case-insensitively; `sort:"started"` is newest first,
      memory/cpu largest first with unknown values last; `cmd` = argv joined with spaces, `audit.redact_text`, cut to 300
      chars (299 + "…"); `managed` from `rt.managed_pids()` (jobs, services and `wsl-keepalive:<distro>`).
    - `proc.kill`: unknown/exited pid → `not_found`; `protected` = the node pid and all its ancestors ("hive node", so
      pid 1 is refused as `protected_process` before the pid ≤ 4 rule) ∪ `rt.managed_pids()` ∪ `rt.keepalive_pids()`;
      the `check_kill` extras go into `audit_extra` with `pid`, `name`, `killed`, `still_alive`; `grace` is capped to the
      call's remaining time − 5 s; `tree:false` stops only that pid (SIGTERM → grace → SIGKILL, method `signal`; Windows
      `taskkill` / `taskkill/F` / `terminate`).
    - Linux `/proc/<pid>/cmdline` shows the parent's command line between fork and exec; tests wait for the exec.
    - WSL keepalive: backoff 10 s doubling to 300 s after each death or failed spawn, reset after 300 s of uptime; the
      state file is `state/wsl-keepalive-<distro>.json` `{distro, pid, ctime, argv, started_at}`; adoption by pid + ctime.
    - `wsl.parse_list_verbose` maps the common localized state words (zh/de) to Running/Stopped; unknown words are kept.
K7. **Interpretations (install, restart, start-hive.sh):**
    - `--system` exists only for systemd (others → `bad_request`); the system unit has `User=` and
      `WantedBy=multi-user.target`; `Type=simple` is added; `--no-capture-path` omits the PATH line (systemd/watchdog) and
      the `path_prepend` capture (schtasks; node only, via `config.edit_config`). ExecStart/WorkingDirectory values are
      quoted only when they contain whitespace, quotes, backslashes, `;` or `$` (`$` → `$$` in ExecStart).
    - Task XML is written UTF-16 with BOM to `<data>/run/task-<kind>.xml`; the LogonTrigger and Principal name
      `%USERDOMAIN%\%USERNAME%`.
    - Watchdog: only the node script calls `_update` (the hub has no relay block); `run/watchdog.pid` (+
      `run/watchdog.json` with the ctime); a live watchdog is never started twice (a re-install rewrites the script
      atomically and notes it). `uninstall` stops the daemon through `control.json` first (exit 0 ends every method's
      loop), then signals only the watchdog shell (never its tree: shims are descendants of the node);
      `stop_services` runs `services.panic_offline` (jobs too).
    - `restart()`: nothing running and nothing installed → `unavailable` "start it: hive <kind> run (or hive service
      install <kind>)"; a foreground daemon is stopped through `control.json` and then reported as `unavailable` "start it
      again: hive <kind> run" (CLI exit 4); old holder still alive after 30 s → `timeout`. `dry_run` returns the plan only
      (a dry run cannot observe a relaunch); the §13.1 sequence is tested with patched seams (`_holder`, `_execute`,
      `_clock`, …).
    - `check_launcher` runs with `PYTHONPATH` removed and cwd = data dir and requires exit 0 plus this version in the
      output; the editable-install test builds a venv with a `.pth` pointing at the checkout.
    - `start-hive.sh`: `HIVE_RELAY_PROXY` values `auto`/empty fall back to `HIVE_PROXY`, `off` means direct; the loop
      appends to `$HIVE_HOME/node/logs/stdout.log`; `HIVE_PYTHON` and `HIVE_POD_WORKSPACE` are test seams. Its inline
      bootstrap contains `ssl.CERT_NONE` on the pin path (§5.9; `deploy/` is outside the package grep gate of §22.3).
    - `deploy/systemd|launchd|windows/*.tmpl` are reference copies for the README; `install.py` renders its own text.
K8. **Interpretations (doctor):** statuses are `ok|warn|fail`; the design's INFO findings (docker group, `python3` only,
    power plan unchecked with interop off) are `ok` rows whose detail starts with `INFO:`. Doctor loads the configs, runs
    the config/files groups first and only then builds the node `Policy` + `SecretStore` (SecretStore repairs a loose
    `secrets.json`, which would hide it) and calls `proctree.set_trusted_path(policy.trusted_path)`. Network GETs use
    `netutil.guarded_request` with a NetGuard (loopback/private/public) built from the config's `network` block (5 s each,
    in parallel); hf-mirror.com is probed only when huggingface.co fails and no endpoint is set. "Forwarded requests on
    the main port" comes from `logs/<kind>.log`; "no X-Forwarded-For" from recent audit rows whose ip is `proxy:<peer>`.
    Windows ACL checks parse `icacls` output for Users/Authenticated Users/Everyone incl. common German/French/Spanish
    names and SIDs; `powercfg` values are the last two hex numbers (AC, DC) because the labels are localized. The
    "hub's machine" for T5 = direct nodes with a loopback, own-hostname or own-tailnet URL plus nodes tagged
    `wsl-host:<hub hostname>`.
K9. **Open items (WP4):**
    - Windows/macOS-only paths (Toolhelp+CIM process listing, `netstat`, GetSystemTimes/GlobalMemoryStatusEx/disk
      ctypes, `schtasks`, `launchctl`, `icacls`, `netsh`, `powercfg`, system_profiler) are written against the design but
      only their parsers and off-OS guards run in this CI.
    - The WSL mirrored-networking Hyper-V firewall spike was not possible here; doctor prints
      `New-NetFirewallHyperVRule … -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -LocalPorts <port>` (Microsoft's
      documented WSL rule) as the fix, to be confirmed on a real Windows 11 machine.
    - Not implemented: (doctor's "service code dir skipped" warning: done in the review fixes, R4-7), §20.5 item 15 (restart
      warning about running `hive.exe` processes), the `--deep` Docker GPU run was not exercised (no daemon here).
    - CLI wiring of `hive service install|uninstall|status` and `hive doctor` is WP6b.

## WP5b review fixes

(Items numbered R5b-1… so they cannot collide with the notes of parallel review packages.)

R5b-1. **CONTRACT-REQUEST (WP5b → P0 `util.py`, additive):** `util.ClosingIterator(source, on_close=None)`: an iterator
    whose `close()` closes the source and runs `on_close` exactly once, also when iteration never started (a bare
    generator's `finally` is skipped by `close()` before the first `next`, so a client gone before the response head
    leaked inflight slots, sockets, audit records and the dist semaphore). Exhaustion or a source error closes it too.
R5b-2. **CONTRACT-REQUEST (WP5b → WP3b `netutil.py`, WP5a `transport.py`, one line each):** `netutil._stream_body`
    (used by `open_stream` and `open_download`) and `HttpTransport.open_stream` return their chunk generator wrapped in
    `ClosingIterator(gen, conn.close)`, so closing an unread stream closes the connection. Callers see the same
    iterator API (`__iter__`/`__next__`/`close`).
R5b-3. **Gateway (amends B5):** every gateway error (authorize, forward, relay `model.forward` errors passed on) has its
    `hint` folded into the OpenAI-shaped `message` as "`<message> (<hint>)`" (the OpenAI shape has no hint field), so
    the §8.7 "the hub token on this node needs the net class for the gateway" and the §7.5 v8 relay-role hint reach the
    app. The hub replaces a direct node's 404 only when its body is the node gateway's own
    `{"error":{"code":"not_found","message":"service X not found on N …"}}` (read up to 64 KiB); any other 404 (an
    upstream model server's "model does not exist", a node's "service X has no OpenAI endpoint") is passed through
    unchanged. Streaming responses (hub and node) are `ClosingIterator`s: slot release, upstream close and the audit
    record run once even when the server closes the stream before the first chunk.
R5b-4. **Hub (amends B4):**
    - Relay register keeps `node_id` only when it matches `^n_[A-Za-z0-9_-]{4,64}$` (the config pattern) and differs
      case-insensitively from every configured node name, alias and other link's node_id; otherwise it stores `""` and
      writes a `warning` audit record `relay node_id refused` (a pod could otherwise take over routing for another node,
      §8.1 resolves node_id before aliases).
    - `max_poll_bytes`: the hub stops at the first task that does not fit (FIFO, §7.2); the free-slot and
      `model.forward` slot-reserve skips are unchanged.
    - While `identity_changed`, a poll gets no tasks, no cancels and no `refresh` (they stay for the bound instance), and
      `POST /v1/hub/nodes/{name}/restart` answers `queued:false`.
    - `DirectLink.ensure_fresh`: callers that waited for an in-flight probe share its failure (`unavailable connect`)
      instead of probing again (one probe in flight per link, §8.3 v5); a success is seen through the fresh row.
    - `GET /v1/dist/hive.pyz` streams the body (256 KiB chunks, `Content-Length` set) and holds the concurrency slot
      (≤ 4) until the body is sent or the client is gone.
R5b-5. **Relay agent (amends B6):** a task id stays in `running` until its result POST finished (incl. retries), so a
    poll during a slow upload does not make the hub mark it lost; its slot stays counted until then.
R5b-6. **dist (amends B3/B8):** `add_node_instructions(..., host=None)` (additive): `host` is the hub address a remote
    machine uses when the listen host is a wildcard or loopback (the WP6b CLI fills it from `--host`/the LAN address).
    `--via` omitted on a `"tailscale"` hub = `--via tailnet`: variants T (+A with `public_url`) and then C. A join code
    never contains a loopback/wildcard host: a tailscale hub whose tailnet IP cannot be read → `bad_request` "cannot
    determine the hub's tailnet IP" (hint: start tailscale, or `--via public`); a T/B code for a wildcard/loopback
    listen host without `host`/`public_url` → `bad_request` (hint: give the hub address or set `public_url`). IPv6
    literals are bracketed in URLs.

## WP3b review fixes

(Items numbered R3b-1… so they cannot collide with the notes of parallel review packages.)

R3b-1. **`_download` file mode (amends W6):**
    - Range resume has a validator: when headers arrive for a fresh body the child writes `<path>.hive-part.json`
      (0600, `{"v":1,"url","etag"(strong only),"last_modified"}`) next to the part. A part is resumed only when that
      sidecar names the same URL, with `If-Range: <etag|last_modified>` (a changed resource answers 200 → restart from
      zero); a part with no sidecar (unknown origin) or another URL restarts from zero. The sidecar is deleted with the
      part on success and kept with it on a sha256 mismatch. The handler also vets `<path>.hive-part.json` for writing.
      Dir mode (hidden per-run part) and pull mode (commit-pinned URLs, every file hashed) send no validator.
    - A 206 is a fresh body only when its Content-Range starts at 0 (or, with no Range sent, has none); a 206 to a
      `Range: bytes=N-` (N > 0) with a missing/unparsable/other Content-Range drops the part and fetches again without
      Range (`_Restart`), never splices or truncates.
    - A stop that surfaces as `cancelled` from `open_download` (or any error while the stop event is set) ends with
      exit 7 "stopped", not exit 1.
    - Server-chosen names: `choose_name` falls back to `download.bin` when the name is longer than 251 bytes in UTF-8
      (255 = NAME_MAX minus room for `-999`; UTF-16 units on Windows) or, on Windows, contains `<>"|?*`. If the
      filesystem still refuses a candidate at publish time (not `EEXIST`), the child publishes as `download.bin`; if
      that fails too the job fails and the hidden `.hive-download-<run>.hive-part` is deleted.
R3b-2. **`_download` pull mode (amends W6):** `Puller._dest` checks `realpath` of the would-be parent against the target
    first and then creates missing folders one at a time, each only after its parent's realpath was checked (nothing is
    created behind a planted symlink); a component that exists and is not a folder fails that file. `.hive-pull.json`,
    the lock file name and `*.hive-part` are never destinations (skipped_unsafe in the child). `run_pull` refuses
    (exit 1) a symlinked `lock_file` or manifest and pre-creates the lock with `O_NOFOLLOW` (where available) before
    `FileLock` opens it.
R3b-3. **model.pull (amends W8):** repo names equal (casefolded) to `.hive-pull.json` / `.hive-pull.lock`, ending in
    `.hive-part`, or with a segment the local filesystem cannot hold together with the `.hive-part` suffix (> 245 bytes
    UTF-8; UTF-16 units and `<>"|?*` on Windows) are `skipped_invalid`. Destinations that collide under
    `EffectivePolicy.path_key(dest, follow=False)` (casefolded where PathGuard casefolds) keep the first listed file;
    the others are skipped as unsafe (`skipped_unsafe` / the dry-run `note`). A target whose `.hive-pull.lock` or
    `.hive-pull.json` is a symbolic link → `conflict`. Audit `extra` gains `route` and `dns` of the endpoint
    (`check_url(endpoint, purpose="download")`, best effort).
R3b-4. **net.download (amends W9):** folder mode also vets `<folder>/download.bin` with `resolve_path(write=True)`, so the
    rules that look at a file's parents (v7 venv `bin`/`Scripts`, PATH dirs, service code dirs) refuse a server-named
    download into such a folder for a gated identity (`policy_denied` `exec_path`) as they refuse fs.write there.
    Audit `extra` gains `route` and `dns` of the vetted first hop.
R3b-5. **netutil:** the unread-stream close fix is R5b-2 (`ClosingIterator`); `_download._fetch` already closes the
    stream in its `finally` before `_HttpStatus` / `_Restart` propagate. A regression test checks that closing an unread
    `open_download` 404 stream closes the connection.

## WP4 review fixes

(Items numbered R4-1… so they cannot collide with the notes of parallel review packages. Regression tests:
`tests/test_wp4_review.py`.)

R4-1. **proc.kill (amends K6):** with `tree:true` (default) the kill guard runs on every process the tree kill would
    signal: `proctree.descendants(pid)` plus, on POSIX when the pid leads a process group (`killpg`), the other members
    of that group. The first refusal refuses the whole call (`policy_denied`, the descendant's rule, `details.descendant`,
    hint "use tree:false, or job_stop/service_control"); nothing is signalled. `protected_pids` walks the node's
    ancestors over one ppid snapshot on Windows (psutil | Toolhelp32) instead of one `process_info` per ancestor; on
    Windows a tree check with several descendants reads them from one `procinfo.list_processes` listing.
R4-2. **proc.list (amends K6):** `filter` matches `name + " " + audit.redact_text(<whole joined cmdline>)` (the text
    `cmd` shows, not cut to 300 chars), so `total`/`shown` never reveal a redacted secret.
R4-3. **sysinfo disks:** `parse_mounts(text, *, wsl=False, isdir=None)` (additive): with `isdir`, mount points that are
    not folders are dropped. `disks()` checks "is a folder" inside the statvfs thread (Docker's `/etc/hosts`,
    `/etc/hostname`, `/etc/resolv.conf` file binds are not disks) and keeps one row per block device + totals (a
    device bind-mounted at several places, as pods do, is listed once; overlays as before).
R4-4. **WSL keepalive (amends K6):** a live recorded keepalive is adopted only when its recorded `argv` equals the
    entry's current argv; otherwise it is stopped (`proctree.kill_tree`, grace 2 s) and the new command started.
    `wsl.read_keepalive_state(state_dir, distro)` / `wsl.keepalive_state_path(state_dir, distro)` (additive) read the
    state file offline (`running` = pid + ctime alive); doctor's Windows wsl group adds ok "keepalive for X running
    (pid N)" or warn "keepalive for X not running" (fix "hive node restart") per `wsl.keepalive` entry.
R4-5. **install (amends K7):** `install(..., system=True)` with `SUDO_USER` set (≠ root) renders the unit for that user's
    HIVE_HOME: the config's HIVE_HOME root when the config path is `<home>/<kind>/config.json`, else an explicit
    `HIVE_HOME`, else `~<SUDO_USER>/.hive` (`pwd`); the default config path follows that home. A logs folder the
    installer creates for a system unit is chowned to the user. `restart()` of a daemon with no install record waits
    ≤ 20 s for a new holder after the old one exits (the `start-hive.sh` loop relaunches without an install record)
    and reports success when one appears; only then is it `unavailable` "start it again: hive <kind> run".
R4-6. **start-hive.sh:** the inline bootstrap runs as `"$PY" -I -` (isolated: no cwd/user site on `sys.path`, PYTHON*
    ignored), so a planted `json.py` in the start cwd is never imported (D56).
R4-7. **doctor (amends K8/K9):** wsl mirrored + `listen.host "tailscale"` shows the IP from `httpserver.tailscale_info()`
    (a tuple; `HiveError` = unavailable); the "service code dir skipped" warning is implemented (services.json read
    offline through a never-started `ServiceManager.policy_view()`; dirs that normalise to ~, a root or an ancestor of
    one → `policy` warn "service code dir X is your home or a root; Hive cannot protect it from writes");
    `parse_icacls(text, path=None)` strips the path from the first line (a profile with a space hid open ACEs there);
    the "without X-Forwarded-For" warning only when no main-port forwarding was logged and `listen.proxy_port` or
    `trusted_proxies` is set (main-port forwarding records `proxy:<peer>` with or without XFF); the Windows
    permission fix is `icacls "<path>" /inheritance:r /grant:r "*<user SID>:F" /grant:r "*S-1-5-18:F"`
    (`doctor.win_fix_perm`, `(OI)(CI)F` for folders; §20.5 item 12).

## WP6b hand-off (hub /mcp, remaining CLI, pyproject, examples)

(Items numbered S1… so they cannot collide with the notes of parallel packages.)

S1. **CONTRACT-REQUEST (WP6b → WP6a test `tests/test_cli.py`):** `test_version_and_wp6b` no longer expects "not
    available yet (WP6b)": `hive gateway url a b` with no hub config on the machine exits 2 with "no hub config". The
    `WP6B` dispatch list in `cli.main` is gone.
S2. **Additive names (WP6b):**
    - mcp: `http_handler(core_factory, *, sessions=None, default_profile="standard", call_wait=None)` returns the raw
      route function (with a `sessions` attribute); `core_factory(identity, request, *, profile, wait_budget) ->
      McpCore`; an optional `McpCore.rebind(identity, request)` attribute is called on every request (§18.5 "rebound to
      the freshly authenticated Identity"); `HttpSessions` (`create/acquire/release/drop/close_all`, injectable clock),
      `HttpSession`, constants `SESSION_IDLE` (3600), `SESSIONS_PER_TOKEN` (16), `SESSIONS_TOTAL` (256), `HTTP_WORKERS`
      (4 pool threads per session).
    - tools: `register_agent_routes(..., mcp=True)` builds the session core (server-hosted Toolkit over the target, so
      `local:` is refused and `fs_transfer` uses a hub-process TransferManager; `watch=False`), registers `POST` and
      `DELETE /mcp` (mode raw) and sets `server.mcp_sessions` (tests/diagnostics).
    - cli: `hive hub add-node … --host H` and `hive hub rotate-token … --via/--host` (passed to
      `dist.add_node_instructions(host=)`, R5b-6); `hive approvals approve … --yes`; test seams `cli._now`/`cli._sleep`
      (the `restart-node --outdated` waits, `audit tail --follow`).
S3. **/mcp interpretations (§18.5):**
    - Only `POST` and `DELETE` are registered, so `GET /mcp` gets the router's 405 (before authentication).
    - Status codes: `initialize` → 200 + `Mcp-Session-Id`; a notification or a client response → 202 with an empty body;
      a request without `Mcp-Session-Id` (other than initialize) → 400 JSON-RPC `-32600`; an unknown, expired, evicted or
      other token's session → 404 JSON-RPC `-32001` "unknown session"; malformed JSON → 400 `-32700`; a batch or a
      non-object → 400 `-32600`; an unsupported `MCP-Protocol-Version` → 400; a bad `profile`/`wait_budget` query on
      initialize → 400 `-32602`. Every response body is `application/json`. POSTs need `Content-Type: application/json`
      (the ApiServer rule; 415 otherwise); Origin/Host checks are the ApiServer's.
    - `profile`/`wait_budget` are read on `initialize` only and fixed for the session (`wait_budget` clamped to
      [10, 280]; absent → §17.4 order without stdio: `HIVE_WAIT_BUDGET` of the hub process, else 45).
    - Session limits evict the least recently used session (per token at 16, in total at 256); evicted/expired sessions
      are closed (in-flight calls cancelled, their pool stopped).
    - `tools/call` runs on the session's pool and the POST waits for its result (cap `max(300, 3 × wait_budget + 60)` s,
      then `-32603` and the call is cancelled). A `notifications/cancelled` for it (another POST) makes the waiting POST
      answer `-32800` "request cancelled" (HTTP needs an answer; stdio sends none). `tools/list` refreshes the Toolkit
      first, since `list_changed` is not delivered over HTTP.
S4. **CLI interpretations (§19):**
    - `join`: the code comes from the argument, `-` (stdin) or `HIVE_JOIN` (a code on argv prints a ps/history
      warning). Before writing, the hub is checked with `relay.relay_transport(...)` `GET /v1/health` **without a token**
      (system CAs or the join pin; `relay.proxy` = `--relay-proxy` / `HIVE_RELAY_PROXY`): a pin or plaintext-policy
      refusal aborts, an unreachable hub only prints a warning. On a new config the consent is rewritten to
      `{"by":"join","at",…,"text":"consent given by join code"}` and `HIVE_HF_ENDPOINT` is written to `models.hf_endpoint`;
      `--proxy` defaults to `HIVE_PROXY`. `--install` uses the watchdog method on pods, `auto` elsewhere.
    - `hub add-node --relay` builds the printed block (join codes, pyz sha256 of `dist.build_pyz()`) before writing the
      entry, so a `bad_request` (no reachable hub address) leaves nothing behind; `--direct --generate` stores the token
      in the hub config and prints only `hive node token add hub-<hub> --kind hub --sha256 …`.
    - `hub rotate-token NAME` also covers direct nodes (new hub token; the node-side revoke/add lines are printed) and
      agent/models tokens (new token printed); the owner token is refused ("hive init hub --force").
    - `hub ack-node`: preview via the owner route, y/N (`--yes`), bind with the shown `pending_sha256`; with the hub down
      (connect refused) it edits `relay_ids.json` under `hub.lock` with the same refusal rule (`bound_seen_at` later than
      the attempt).
    - `hub add-agent` prints the token once, the hub URL for other machines (tailnet IP, LAN hostname for a wildcard
      bind, the listen host, `public_url`; 127.0.0.1 only as a last resort) in an `init client … --default` line, the
      `mcp-config` hints, a `claude mcp add --transport http` line only when an https base exists, and the T5 warning
      when the role has exec, no approvals are required and its node globs match a hub-machine node (loopback /
      own-hostname / own-tailnet direct URL, or tag `wsl-host:<hub hostname>`; same rule as doctor K8).
    - `gateway token add` defaults to `--hub` when a hub config exists here; hub globs must be `node/service`, node
      globs plain service names. `gateway url` prints, per base, the "Provider … Base URL …/v1 …" line and the
      `/v1`-less line; bases: tailnet IP (+ MagicDNS alternative) for a `"tailscale"` hub, the LAN hostname for a
      wildcard bind, the listen host otherwise, `public_url`, and (on the hub machine) 127.0.0.1 and host.docker.internal;
      `--connection C` uses that connection's URL; `--node` this node's own port; `--token-file` lists the served model
      ids from `GET …/v1/models` (first base on this machine).
    - `approvals`: list/grants/revoke run against `this-pc-owner` and/or `hub-local` (whichever exist in client.json, or
      the one `--node`/`--hub` names); a daemon that does not answer prints "node not running"/"hub not running".
      `approve`/`deny` look the id up (`include_done=1`) in both places, print the review `lines` (escaped, paged by 40
      on a TTY) and ask y/N (`--yes` skips); `--for` takes `Nm`/`Nh` → `grant_minutes`.
    - `mcp-config`: `whoami` decides owner vs agent; when the server is unreachable, `this-pc-owner`/`hub-local` still
      count as owner connections. `--write` merges `mcpServers.hive` into the Claude Desktop config
      (`~/Library/Application Support/Claude/…`, `%APPDATA%\Claude\…`, `~/.config/Claude/…`) or `~/.cursor/mcp.json`,
      keeping a `.bak`. `--remote` prints the HTTP form (with the connection's token and a warning).
    - `doctor` (no kind: node if a node config exists, else hub, else client) exits 1 when any check fails; `audit tail`
      reads the daily files locally (`--hub` or a hub-only machine → hub audit); `push`/`pull`/`logs` use the
      `this-pc-owner` default like `call`/`sh`; `logs -f` polls with `wait: 10` and ends a job's stream once it is final.
    - Exit codes stay `exit_code_for` (a `bad_request` from an action/library is 1; CLI usage errors are 2).
S5. **pyproject.toml:** setuptools build, `name = "hive-agent"`, version from `hive.__version__`, no dependencies,
    extra `full = ["psutil"]`, script `hive = hive.cli:main`, packages `hive`, `hive.actions` (verified with a venv
    `pip install ./hive`; `hive --version` works). `.gitignore` already ignores `build/` and `*.egg-info/`.
S6. **CI:** `.github/workflows/hive-tests.yml` at the repository root (WP6 per §3/§21; outside the `hive/` folder,
    committed explicitly): ubuntu/windows/macos × 3.9/3.13 plus psutil on ubuntu/windows, `pip install -e ./hive`, the
    unittest discover from `hive/`. Not exercised here.
S7. **Open items (WP6b):** the README is a first version for the lead's review; Windows/macOS-only paths (Claude
    Desktop config locations, `_console_command` on Windows) are written but only run here through mocks; `service
    install` from a bare source checkout is refused by WP4's `check_launcher` (expected: install with pip first);
    E2E M2 (hub + relay + join + `/mcp` with real processes) remains the lead's.

## WP6b review fixes

(Items numbered R6b-1… so they cannot collide with the notes of parallel review packages. Regression tests:
`tests/test_wp6b_review.py`.)

R6b-1. **/mcp sessions (amends S3):** the per-token (16) and total (256) caps evict the least recently used **idle**
    session (`active == 0`, §18.5 "the oldest idle one"); a session with a request in flight is never evicted. When a
    cap is reached and every candidate is busy, `HttpSessions.create` raises `HiveError("unavailable")` without
    evicting anything and the `initialize` POST answers HTTP 503 (`Retry-After: 10`) with JSON-RPC `-32603`.
    Closing a session (DELETE, idle expiry, eviction) also calls the session Toolkit's `close()`, which cancels its
    hub-process `fs_transfer` copies (part files stay for a resume).
R6b-2. **/mcp body (amends S3):** the body passes `httpserver.json_depth_ok` first (else 400 `-32600` "JSON nested too
    deeply"); a `RecursionError` from the parser is the same 400.
R6b-3. **McpCore (stdio and HTTP):** a `tools/call` whose id is already in flight in the same session is answered
    `-32600` "duplicate request id in flight"; a finishing call removes its `inflight` entry only if it is still its
    own handle.
R6b-4. **CONTRACT-REQUEST (WP6b → WP6a `tools.py`, WP5b `transfer.py`), additive:** `Toolkit.close() -> int` (cancels
    the Toolkit's TransferManager transfers; 0 when none was started) and `TransferManager.cancel_all() -> int`
    (cancels every running transfer). Used by R6b-1 only.
R6b-5. **mcp-config fails closed (amends S4, §5.1, §18.4):** the token kind comes from `whoami`; with the server
    unreachable, from the configs on this machine (the connection token's hash against node `auth.tokens` and hub
    `tokens`; an owner match → owner), and `this-pc-owner`/`hub-local` still count as owner. Owner → exit 3 unless
    `--allow-owner`; a kind that stays unknown → exit 4 ("cannot check the token…") unless `--allow-owner`.
    `--remote` needs a reachable server whose `whoami.server.role` is `hub` and that serves `/mcp` (a session-less
    `POST /mcp` probe: 404 = `listen.mcp_http` off) — else exit 2 "remote MCP is served by a hub only; use the stdio
    snippet" (exit 4 when unreachable), and an `http://` URL whose host is not loopback/private/tailnet
    (`transport.plaintext_ok`) is refused (exit 2). Nothing is printed with the token in those cases. `--write` keeps
    the `.bak` private (0600, `util.atomic_write_bytes(private=True)`).
R6b-6. **hub add-agent T5 (amends S4):** the warning is printed unless every exec action (`shell.run`, `job.start`,
    `service.define`, `service.start`, `service.restart`) is denied or approval-gated by the token (or the role has no
    exec), so a narrow `--approval fs.delete` still warns. This covers the §5.2 `auth.exec_gated` rule (shell.run) and
    also job/service starts, which run commands too.
R6b-7. **hub add-node --direct (§14.3, C1):** an `http://` URL must pass `transport.plaintext_ok(host,
    resolve_host(host, port))` (loopback, private/tailnet IPs, local or `*.ts.net` names), else exit 2 with "use the
    node's tailnet/LAN address, or https with --tls-pin" and nothing written or printed; `https://` to a non-private
    host without `--tls-pin` prints a warning (system CAs / `network.ca_file` may still be fine).
R6b-8. **hub restart-node --outdated (amends S4):** after the online wait every outdated `hub.nodes` row is named:
    direct nodes "direct node: upgrade by hand", offline relay nodes "offline: upgrade by hand when it is back",
    relay nodes without auto_update "auto_update off: upgrade by hand"; restarts are queued only for online
    auto-updating relay nodes. Node-supplied versions are escaped.
R6b-9. **gateway (amends S4):** `gateway token add` with a non-glob first model prints the app settings block (the
    `gateway url` bases; "API key: the API key above" so the token itself appears once; Model `<model name>`) plus a
    `gateway url … --token-file FILE` pointer for the served ids; glob entries keep the pointer only. `gateway url`
    escapes the model id it puts into the Provider line (§19).
R6b-10. **approvals list:** when every place fails, it prints the per-place lines but not "no pending approvals" and
    exits with that failure's code (4 for "not running"), like `approvals revoke`.

## M2 integration (lead, §22.1 step 5)

(Items numbered M2-1… . Tests: `tests/integration/test_e2e_m2.py` (E2E 1), `test_e2e2_restart.py` (E2E 2),
`test_e2e3_security.py` (E2E 3), `test_e2e_gateway.py` (the §21.2 gateway list end to end), helpers `m2util.py`.)

M2-1. **Real processes, not one process.** §21.2 says E2E 1 runs "in one process"; the M2 tests instead drive real
    daemons on loopback (`hive hub run`, `hive node run`, the downloaded `hive.pyz node run`), each under a
    restart-on-75 loop (`m2util.Supervisor`, the watchdog/D55 rule), plus the CLI, the Python client, an MCP stdio
    subprocess and the hub's `/mcp`. In-process hub + direct + relay coverage stays in the WP5 tests
    (`tests/test_hub.py`, `test_relay.py`, `test_gateway.py`). A join code never names a loopback hub (R5b-6), so the
    tests use `hub add-node … --relay --host 10.255.0.1` and point the decoded code's `u` at the loopback port.
    CI containers run as root: the tests set `allow_root` in the written configs (§13.1).
M2-2. **CONTRACT-REQUEST (M2 → WP6 `tools.py`, behaviour fix):** an `fs_transfer` call whose copy ended `failed`
    within the wait (e.g. the source holds a node secret value, §15.3 v7 `masked`) returned `isError:false` with the
    text "transfer … failed … error: …"; §18.2 says execution errors are `isError:true`. `_run_fs_transfer` now marks a
    `failed` status as an error result (rendering unchanged; `running`, `done` and an explicit `cancelled` stay
    non-errors).
M2-3. **CONTRACT-REQUEST (M2 → WP5 `gateway.py`, audit only):** (a) gateway audit rows name the canonical node: the
    node backend wrote the URL segment (`"node":"self"` on every hub passthrough row), the hub wrote an alias as given;
    new optional backend method `audit_node(node) -> str` (node: its own name; hub: `hub.resolve(node).name`, unknown
    names unchanged), used only for the audit row. (b) the hub's direct-link passthrough recorded `usage: null` for
    every non-streamed reply (only SSE tails were scanned): replies of ≤ `USAGE_SCAN_MAX` (1 MiB) that are not SSE are
    now parsed for `usage` once the stream ends (§8.7 step 5 lists usage). Nothing changes in what the app receives.
M2-4. **(M2 → WP6 `__main__.py`, robustness):** `hive … | head` printed a `BrokenPipeError` traceback; the CLI entry
    now exits 1 quietly (stdout redirected to devnull, the Python docs recipe).
M2-5. **WIP fix kept (M2 → WP2 `jobs.py`):** `JobManager._save` skips a record that was removed meanwhile (a finished
    `shell.run` whose run dir was already deleted) instead of logging "cannot write job record" (seen as log noise in
    the E2E 1 node log).
M2-6. **Interpretations in the tests (no code change):**
    - E2E 3 "readonly token sees no mutating tools": §17.2 filtering keeps `fs_op` with `op` enum `["stat"]`,
      `fs_transfer` (its `fs.read_chunk` side is allowed; every node-side write is refused) and `call_action` (only
      read actions reach the node). The test asserts that no other mutating tool is listed and that `fs_op` is
      narrowed. A server-hosted Toolkit (node `GET /v1/tools`, hub `/mcp`) has no `local:` endpoint, so `fs_transfer`
      is useless to a readonly token there; hiding it when every reachable write side is denied would be a §17.2
      change (lead).
    - E2E 3 "docker preset with volume `~`": refused as `policy_denied` rule `contains_denied` (`~` holds Hive's
      `.hive` and secret paths), not a docker-specific rule. A fake `docker` on a trusted PATH dir makes the define
      reach the volume check.
    - E2E 3 "interpreter's site.py": only meaningful when the stdlib is inside an rw root, so the node gets the
      stdlib's parent as an extra root and the test writes the file's own bytes (harmless if a check regressed). The
      path must be given resolved: §5.4 step 4 is a lexical pre-check (uv's `cpython-3.9-…` dir is a symlink).
    - Gateway v8 "a models token for desk/llm asking for desk/other → 404": §8.7 step 1 refuses a pair outside the
      token's globs with 403 first; the test accepts 403 or 404 there (as WP5's test does) and asserts that the
      message names no other service or node; an unknown node is 404.
    - E2E 2 reboot: simulated by stopping the node (exit 0), killing every shim/child it started and rewriting the
      stored `boot_key` of each service's `runtime.json`: the autostart service restarts, the other is `stopped` with
      the §10.5 "not restarted after reboot" error, the running job is `lost`. A crash in the same boot (SIGKILL of
      the daemon and of one service shim) restarts that service; a surviving shim is adopted.
M2-7. **M2 observations (no change made):**
    - `hive hub add-node NAME --relay` on a hub that listens on 127.0.0.1 exits 2 ("another machine cannot reach")
      unless `--host` is given; with `--host <private IP>` it prints the tailnet `hive join` block, not the pod
      one-liner (that needs `public_url`, §20.4). The pod one-liner itself (`GET /v1/dist/hive.pyz` without a token,
      `X-Hive-Sha256` = the local deterministic build, `join --pod`, `_update`, `node run`) works.
    - `hive node restart` on a foreground daemon without a loop answers exit 4 "the node stopped, but it ran in the
      foreground" after ~20 s (per §13.1); under a loop it reports "node restarted" in < 1 s.
    - `hive gateway url NODE SERVICE --token-file F` still prints the "no token yet? hive gateway token add …" line.
    - The node lists `HF_TOKEN` from its own environment in `secrets` (§5.13 env secrets) although `node secret list`
      (store only) is empty; correct, but owners may find the two lists confusing.
    - The hub probes direct links every 30 s (`PROBE_INTERVAL`), and `hub.nodes` reports the last probe: a node that
      starts a moment after the hub shows `offline` for up to 30 s (calls still probe synchronously, §8.3 v5). The
      M2 tests start nodes before the hub.
    - `hive join` without `--pod` keeps the node listener on the default `127.0.0.1:7701`; two joined nodes on one
      machine (tests running in parallel) collide, so the tests move it to a free port.

## M3 integration (lead, §22.1 step 6)

(Items numbered M3-1… . Tests in `tests/integration/`: `test_e2e_m3_pull.py` (model.pull), `test_e2e_m3_netproxy.py`
(netproxy routes + fake-ip), `test_e2e_m3_docker.py` (docker preset), `test_m3_wsl.py` (WSL keepalive, unit level
on a real NodeRuntime), `test_e2e_m3_tools.py` (every `full` tool + the counts); helpers `m3util.py`
(`NodeDaemon`: `hive init node` + `hive node run` under m2util's restart-on-75 `Supervisor`; `McpSession`; a test-only
fake-DNS `sitecustomize`).)

M3-1. **CONTRACT-REQUEST (M3 → WP3 `netutil.py`, behaviour fix):** an absolute-form (plain `http://`) request through
    the proxy that the proxy answered with 407 was mapped to `unavailable` reason `proxy_auth` only when no
    `Proxy-Authorization` had been sent; with configured but refused credentials (`http://user:wrong@…`) the agent got
    the proxy's 407 as if it were the target's HTTP response (`net.http` ok:true status 407, a download failing with
    "HTTP 407"). Now every 407 on such a hop is `proxy_auth` ("the proxy refused the credentials in network.proxy"),
    like `open_tunnel` for CONNECT (§11.4).
M3-2. **CONTRACT-REQUEST (M3 → WP1 `node.py`, behaviour fix):** `node.panic` (run offline by `hive node panic`)
    resolved `docker` and services' stop commands on the CLI process's fail-safe trusted PATH (§10.1: PATH minus
    home/cwd/temp + fixed dirs) instead of the node's (`exec.path_prepend` + PATH minus rw roots, §5.4), so a docker CLI
    reachable only through `path_prepend` (or a PATH dir the fail-safe default drops) was not found and labelled
    containers kept running (`containers: 0`). New private helper `_offline_trusted_path(cfg, data_dir)` builds the
    node `Policy` (as `doctor` does) and calls `proctree.set_trusted_path(policy.trusted_path)` before
    `services.panic_offline`; failures keep the default. The PATH is still the CLI's own environment (the owner's
    shell), as for every offline command.
M3-3. **CONTRACT-REQUEST (M3 → WP1 `config.py`, schema fix):** `wsl.keepalive[].command` was validated as a string,
    but §20.7 step 6 gives it as an argv list (`["/bin/sh","-lc","exec $HOME/.hive/node/run-node.sh"]`, run by
    `wsl.exe --exec` without a shell) and `wsl.Keepalive._argv` only uses a list: the documented form was rejected at
    load and a string was silently replaced by `/bin/sleep infinity`. Now `command` is `null` or an array of 1–64
    non-empty strings (≤ 4096 chars each). `init node --wsl-keepalive` still writes `command: null`.
M3-4. **Interpretations in the tests (no code change):**
    - Fake-ip end to end: the node and its `_download` children get a test `sitecustomize` on `PYTHONPATH` (kept by
      `self_env()`) that answers `getaddrinfo` for listed names only (`HIVE_TEST_FAKE_DNS`); names resolving only to
      `198.18.0.0/15` go through the proxy **by hostname** (absolute-form for http, `CONNECT name:443` for https); a
      literal fake-ip and a fake-ip + private mix are refused; a gated (`--cautious`) token's POST to an all-fakeip
      name → `loopback_write` (rule 5c) while its GET passes and an operator token's POST is proxied by name.
    - `hive node token add` on a `standard` node does not default to `--cautious` (only on a cautious node, §19 v8);
      the tests pass `--cautious` explicitly.
    - The docker E2E uses a stateful fake `docker` (container table, `run` serving an OpenAI stub on the published
      `127.0.0.1` port, `stop`/`rm -f`/`inspect`/`ps --filter label=`/`top`) on a trusted PATH dir outside every root.
      `hive node panic` runs with the node's PATH (the owner's shell normally has it).
    - WSL keepalive: `NodeRuntime.start()` runs with `hive.node.IS_WINDOWS` patched and `wsl.wsl_exe` → a fake POSIX
      `wsl.exe`; `node.info.wsl` is checked with `hive.actions.node.IS_WINDOWS` patched and `wsl.list_distros` mocked.
    - The disk-space precheck uses a stub listing that reports 1 PiB for one file (no `shutil.disk_usage` patch in a
      daemon); the bytes are never requested.
    - "Every tool": `proc_kill` kills a process the test started (not managed, not an ancestor of the node);
      `fs_transfer` uses a `local:` file under `~/Hive` (client.json `local_files.roots` default).
M3-5. **Counts verified end to end:** 38 actions (37 node-scope rows on the node's `GET /v1/actions` + `hub.nodes`);
    tool profiles 10 / 28 / 31 with exactly the §17.2 names through `tools/list` of stdio MCP sessions
    (`--profile minimal|standard|full`, default standard, `--prefix hv_`) and through the hub's `/mcp?profile=`
    (direct link to the node daemon); 4 export formats × 3 profiles through `GET /v1/tools` (§17.7 shapes; `gemini` →
    400); 3 MCP protocol versions (`title` only from 2025-06-18; an unknown version gets a supported one).

## Phase 2 hardening review (whole system)

(Items numbered P2-1… . Regression tests in `tests/test_phase2_review.py`.)

P2-1. **CONTRACT-REQUEST (Phase 2 → WP2 `presets.py`, security fix, §5.6):** `candidate_words` splits POSIX
    commands with `shlex.shlex(posix=True, punctuation_chars=True)` and drops the pure operator tokens, so a script
    next to an unquoted shell operator (`bash run.sh;true`, `run.sh|bash`, `<run.sh`, `(./x.sh)`) is still a
    candidate word and bound (and held) by `bind_files`; PowerShell/cmd words are also split on `; | & < > ( )`
    (a bare `&` is no longer a word). The hub's `_bind_words`/`_split` call it, so hub-routed approvals get the same.
P2-2. **CONTRACT-REQUEST (Phase 2 → WP3a `actions/files.py`, security fix, §5.1):** `fs.search` matches against
    the text after `SecretStore.mask` (whole values) when the node knows secret values, so the match predicate
    (matches / files_matched) is no oracle over secret bytes. Line numbers stay those of the masked text (they differ
    only for a multi-line secret value).
P2-3. **CONTRACT-REQUEST (Phase 2 → WP5b `gateway.py`, WP1 `httpserver.py`):** new `gateway.upstream_status(v)`;
    the relay path (pod-chosen `status`) and the node gateway's upstream status accept only an int 200..599 other
    than 204/304, else an OpenAI-shaped 502 `upstream_error`. `ApiServer._write` (defence in depth) puts only
    100..599 on the wire (else 502) and for 1xx/204/304 writes no body and no Content-Length (a dropped body forces
    `Connection: close`); `_write_stream` maps those statuses to 502.
P2-4. **CONTRACT-REQUEST (Phase 2 → WP5b `hub.py`, D54 addition):** `relay_ids.json` node records carry an
    optional `"node_id"` (readers default null; reset with the record by `rotate-token`). A register's node_id is
    stored only when the register's instance key is the bound one (or binds now); a register without the bound key
    keeps only the stored node_id; a node_id stored for another node is refused even while that node is offline
    (`relay node_id refused` audit as before). Residual: before a pod has ever registered, a pod holding its own
    relay token can still claim that pod's (public) node_id first; owners resolve by name.
P2-5. **Lead (P0 `httpserver.py`):** `ID_PARAM_PATTERNS` end in `\Z` and are applied with `re.fullmatch`
    (`%0A`-suffixed ids/names/nodes → 400 before any handler).
P2-6. **CONTRACT-REQUEST (Phase 2 → WP5b `dist.py`, WP4 `deploy/runpod/start-hive.sh`, §5.9):**
    `add_node_instructions` prints an `http://` join code (variants T/C) only when `transport.plaintext_ok` holds for
    the host (loopback/private/tailnet, names resolved), else `bad_request` with a TLS/public_url hint.
    `start-hive.sh` refuses to fetch `hive.pyz` over `http://` from a host that is not loopback/private/tailnet
    (IP literal by range; a name by its addresses, or a local/`.ts.net` name) unless `HIVE_PYZ_SHA256` is set.
P2-7. **Lead (P0 `util.py`, blocker fix):** Linux `process_create_time` no longer uses the live `/proc/stat btime`
    (it moves with every wall-clock step: NTP first sync on a Pi without RTC, WSL2 resync, local-time RTC). The first
    btime read in a boot is kept per `boot_id` in a 0600 file owned by this uid
    (`$XDG_RUNTIME_DIR`, `/dev/shm` or the temp dir: `hive-btime-<uid>-<boot_id>`, first writer wins via `link`,
    O_NOFOLLOW + owner/mode checked on read) and cached in-process, so every Hive process of the boot (node, shims,
    CLI) computes the same create times and `boot_key()` stays stable. On Linux psutil is no longer consulted for
    create times (its `create_time` follows the moving btime too); other platforms unchanged.
P2-8. **CONTRACT-REQUEST (Phase 2 → WP2 `textlog.py`, §10.3):** `.state` is a seqlock: `{"v":1,"rotated_bytes",
    "seq"}` with an even `seq` when settled; before the renames the writer writes an odd `seq` with `"rotating": S`
    (the rotated file's size). Readers retry while `seq` is odd or changed (≤ 2 s, then settle from the files: the
    rotation counts as done when `.1` has size S and the current file is smaller); a writer that finds an odd state
    at start settles it the same way. `read_range` with an offset beyond the logical size returns empty data with
    `offset = next_offset = ` the requested offset (a follower never moves backwards). Readers of old state files
    (no `seq`) treat them as settled.
P2-9. **CONTRACT-REQUEST (Phase 2 → WP2 `textlog.py`/`actions/shell.py`/`jobs.py`, P0 `util.decode_text`):** on
    Windows, `decode_chunk` holds back a trailing lead byte of non-UTF-8 (ANSI code page) text; new
    `textlog.dbcs_skip(before, data, *, aligned_start=False)` drops a leading trail byte (walking forward from the
    last byte < 0x40 of the preceding bytes), used for the `shell.run` 75 % tail and `read_log` ranges;
    `util.decode_text` retries the code page without the first/last byte (marking them U+FFFD) and otherwise picks
    the `errors="replace"` decoding with fewer U+FFFD, instead of turning a whole chunk into U+FFFD through UTF-8.
P2-10. **CONTRACT-REQUEST (Phase 2 → WP2 `jobs.py`/`services.py`):** an unreadable `jobs/<id>/job.json` is
    renamed `job.json.corrupt` and skipped (warning log); an unreadable `services.json` is renamed
    `services.json.corrupt-<YYYYmmddHHMMSS>`, the manager starts with no definitions and appends a warning to
    `rt.warnings`. Neither makes the manager `_Unavailable`.
P2-11. **Lead (P0 `util.py`, additive):** `atomic_write_bytes`/`write_json_atomic` take keyword `mkdir=True`;
    `JobManager._save` writes with `mkdir=False` and removes the run dir again when the record was removed during
    the write (a removed job never reappears on disk). New `util.is_disk_full(e)` (ENOSPC, EDQUOT, Windows 112/39);
    `JobManager.start_job` maps it to `too_large` "disk full" (hint "disk full").
P2-12. **CONTRACT-REQUEST (Phase 2 → WP2 `services.py`):** restart timestamps later than now + 60 s are dropped
    when counting the restart window and when restoring `runtime.json`. `ServiceManager.shutdown` joins the
    staggered autostart thread (≤ 5 s), and a spawn that completes after the shutdown hands its Popen to
    `proctree.adopt_popens`.
P2-13. **CONTRACT-REQUEST (Phase 2 → WP3 `_download.py`):** `_fetch(…, checkpoint=dict)` keeps the hasher state
    of the part across attempts of one file, so an in-process retry re-hashes the part only when its size or mtime
    changed. A full disk (ENOSPC/EDQUOT/Windows disk full) is not transient: pull files fail with exit 4 at once (so
    `_fatal()` stops the other workers), `net.download` ends with exit 4.
P2-14. **Lead (`transport.py`, `hub.py`, tests):** `_Conn.__del__` and `HttpTransport.__del__` close keep-alive
    sockets (no unclosed-socket ResourceWarnings from thread-local connections of finished threads); `Hub.stop`
    joins its reaper/prober threads (≤ 5 s each) after the in-flight wait and closes the link transports again.
    `test_e2e_m2._shutdown` waits for both nodes online and retries `service.remove` that could not connect.

## Phase 2 release (lead: build, README)

R-1. **Release build verified:** `pip install ./hive` into fresh venvs (3.13 and 3.9) gives `hive --version`,
    `hive version` and `python -m hive --version`; `hive dist build` from the source tree, from either Python and from
    the installed wheel produces byte-identical `hive.pyz` (deterministic, 55 entries, `.py` only). The pyz ran a node
    smoke from a folder without the checkout on both Pythons (`init node`, `node run`, `sh` with the remote exit
    code, `call`, job + `logs`, 38 actions, MCP stdio `tools/list` = 28, `node stop` exit 0) plus a hub smoke
    (`init hub --with-local-node`, `hub status`, `add-agent --cautious`, `gateway token add`/`url`, `sh` via
    `hub-local`). Regression gate: `tests/integration/test_release_pyz.py`.
R-2. **Lead (P0 `util.py`):** `atomic_write_bytes(private=True)` sets 0600 through the open fd on POSIX
    (`os.fchmod`; `mkstemp` already creates 0600) and calls the path-based `secure_file` only on Windows. The
    path chmod raced the removal of a job's run dir during `JobManager._save` (P2-11) and logged a spurious
    "cannot restrict permissions … No such file" warning on every short `shell.run`.
R-3. **README.md** rewritten for the owner (Chinese, beginner level): parts, install per OS (Windows, Linux/macOS
    venv + `~/.local/bin` link, WSL2 §20.7, pods, pyz), the overview's "你需要做的" order as the default hub +
    Tailscale walkthrough, MCP for Claude Desktop/Code/Cursor, gateway for Cherry Studio/Open WebUI, daily
    commands, safety, proxy/mirror, doctor + troubleshooting, upgrade/uninstall, layout. `tests/test_readme.py`
    parses every `hive …` command in it with the real argparse parsers (subcommands and flags; `mcp` and `sh`
    with their own parsers) and checks file links and in-page anchors. Note for README authors: `call`/`sh`/
    `logs`/`push`/`pull` default to `this-pc-owner` when it exists (not to client.json `default`), so hub examples
    pass `--connection`.

## External review fixes

### Beginner README walkthrough (CLI, install, README)

X-W1. **Foreground restart (owner decision A; `cli.py`, `node.py`, `hub.py`, `install.py`):** `hive node run` /
    `hive hub run` started by hand in a terminal (`node.foreground_restart_ok`: stdin or stderr is a TTY, not
    `--relaunched`/`--supervise`, no `HIVE_WATCHDOG=1`, no systemd `INVOCATION_ID`, not the Windows loop's child
    `HIVE_FOREGROUND_CHILD=1`) re-exec themselves after exit 75 (`node.reexec_foreground`: prints "restarting to apply
    new settings…", chdir back to the original cwd, POSIX `os.execv(cli._relaunch_prefix() + CLI args)` (the pyz, the `hive` script from argv[0], else `python -m hive`); Windows runs the
    same command as a child in a loop). Supervised runs keep exit 75. Contract change: the node and hub lock notes are
    `"node <hex>"` / `"hub <hex>"` (per start), and `install.restart` compares holders by `(pid, ctime, note)` — its
    exit wait ends when the pid exits or the holder changes, `_wait_new_holder(ctx, old_holder, …)` accepts any
    different holder. `cli._restart_daemon` prints "applied (restarted the <kind>)", "the <kind> is not running: will
    apply on next start", or for a foreground run without a terminal "… start it again: hive <kind> run"; it returns
    True when a running daemon was restarted. DESIGN §13.1, §8.6, §19 (v8.1).
X-W2. **`init hub --with-local-node` (B; `config.py` init_* only, `cli.py`):** registration moved to
    `config._local_node_entry(hub_name)` (refuses a local node without a listener); `cfg._local_node` names the entry
    (`hubhost` when node and hub names are equal, else the node name). The CLI prints the name and its URL and restarts
    a running local node (`_restart_daemon("node")`), so it loads the new hub token at once (with X-W1 also in a
    foreground terminal). README 4.2 explains the name.
X-W3. **`init hub` on an existing config (F; `config.edit_hub_init`):** `--listen`/`--tailscale`/`--public-url`/
    `--funnel`/`--with-local-node` change only that setting under `edit_config`, tokens kept, a running hub restarted;
    nothing to change -> exit 2 with the `--force` hint (a name clash for the local node is refused before any token is
    minted). `--name`/`--tls-*` on an existing hub print "applies only when creating". `init_hub()` itself still
    raises `conflict` on an existing config without `force`.
X-W4. **`hive service define` / `hive guide` (D):** `service define NAME` maps `--preset/--model/--arg/--argv/--command/
    --port/--endpoint/--cwd/--autostart/--no-start/--wait (30)/--param K=V/--node/--approval` onto `service.define`
    (`--model` -> the preset's `model` or `model_path` arg; exactly one of preset/argv/command); `hive guide [TOPIC]
    [--list]` prints `guides.topic_text`. README section 6.1 (Ollama, vLLM, llama.cpp, a stub via `--argv`).
X-W5. **Agent token on the agent's machine (E):** `--token -` (all commands using `_read_token_arg`) reads one line
    from a pipe or a no-echo prompt (Ctrl-D/Ctrl-C -> "cancelled; nothing was saved", exit 2). `init client --token -`
    saves `tokens/<name>.token` 0600 (or the `--token-file` path, written 0600 by Hive); an existing `--token-file`
    readable by others is chmod 0600 with a note. `hub add-agent` prints the one line
    `hive init client --name <hub> --url <url> --token - --default` and the mcp-config line with `--connection`.
X-W6. **Small items (F):** `init node` warns at once as root/Administrator (explains `allow_root`; not for `--pod`
    or when the config already allows root); `init node|hub --tailscale` runs `tailscale_info()` and warns with an
    install/`tailscale up` hint; `mcp-config` (and the `init node` snippet) put `HIVE_HOME` into `env` /
    `claude mcp add -e HIVE_HOME=…` when it is set and not `~/.hive`; non-TTY `approvals approve|deny` and
    `node panic` print the exact `--yes` command; `hive sh --approval AP` passes `approval_id`, and `sh`/`call` print
    the retry line after `approval_required`; `hive nodes` with one row, no `--connection` and a `hub-local`
    connection that is not the default prints the `--connection hub-local` hint; `node secret set` prompts end cleanly
    on Ctrl-D/Ctrl-C (and refuse an empty value); `node pause` polls `node.info` (≤ 3 s) and says whether the running
    node confirmed; `join --install` follows the node log (≤ `JOIN_VERIFY_S` 30 s, stops `JOIN_FAIL_GRACE_S` 8 s after
    a logged start failure) and prints "joined: …" only after "relay registered with", else exit 4 with the cause
    (port in use -> `--no-listen` / `--listen 127.0.0.1:7721`); `doctor` without a kind checks node and hub when both
    configs exist (JSON rows then carry `target`); the watchdog install notes explain the no-systemd fallback and the
    login-script line in desktop terms; `service define` prints `hive gateway url self NAME --node` for a local node.
X-W7. **README:** upgrade/uninstall rewritten (venv pip first, `service uninstall` + `rm -rf ~/.venvs/hive
    ~/.local/bin/hive`, optional `~/.hive`, Windows and pyz variants); `--yes`; a sample of command output with
    Chinese explanations; foreground restart wording; `hubhost`; `--token -`; Linux Claude Desktop path;
    `service_start` -> `service_control`; approval retry flow; `hive nodes --connection hub-local`; `hive guide`;
    `pip install` leftovers (`build/`, `*.egg-info`). `tests/test_readme.py`'s `sh` parser knows `--approval`.
    Regression tests: `tests/test_walkthrough.py` (incl. a real pty `node run` that survives `node token add` with the
    same pid), plus assertions in `test_cli_wp6b.py`.

### Security audit (approvals, services, jobs, policy, HTTP)

X-S1. **Approval review cwd (finding 1, HIGH; `presets.py`, `approvals.py`):** `presets._review_cwd(policy, cwd)`
    replaces `_resolve_cwd` and never falls back to the workdir for a given cwd: it resolves the cwd like
    `check_cwd` does at run time (`~`/variables, relative -> workdir, realpath; through `resolve_path` without
    `must_exist`), shows it verbatim as `cwd: <given> (= <real>)` plus "(does not exist here yet)" or "(not usable
    here: <reason>)" (none given: `<workdir> (node workdir)`), and binds the command words against it (a script in a
    missing folder is `absent`). Contract change: node-side reviews of shell.run/job.start/service.define/
    service.start/service.restart carry a fifth key `cwd` (the real path; hub reviews do not); `ApprovalStore.require`
    copies it into the record (`rec["cwd"]`) and, on consume, compares it with the fresh review's `cwd`: different
    or missing -> the record is used up and `conflict` "the working folder of the approved command changed; request
    approval again" (`details.changed=[<approved cwd>]`). DESIGN §5.6, T16 (v8.1).
X-S2. **Approved fingerprints survive restarts (finding 2, HIGH; `approvals.py`, `services.py`, `jobs.py`,
    `policy.py`):** `FileHolds.fingerprints(hold_id) -> {real path: approved fingerprint}`. Service run state key
    `approved_files` (persisted in runtime.json; `_new_rs` default `{}`): `define` replaces it with its own hold's
    fingerprints (`{}` without a hold); a manual start/restart (`_spawn` with ctx -> `_set_approved`) re-fingerprints
    the earlier entries (an ungated/owner start re-approves what it runs) and merges the call's hold over them.
    `_system_spawn` (supervisor respawn, backoff, autostart, same-boot restart) calls `_hold_approved`: the stored
    fingerprints are verified and held with `FileHolds.acquire` under the system identity (without `rt.holds`: plain
    compare); mismatch -> no spawn, `_fail(name, APPROVED_CHANGED_ERROR)` (state `failed`, `last_error` "approved file
    changed; approve a start again"); the hold goes to `_spawn(hold=…)` (new kwarg) and is released if the spawn did
    not take it. `_rehold` (adoption after a node restart) acquires against the stored fingerprints; a 1.0.0 run state
    without them (held paths, empty `approved_files`) is migrated with the current content; on a mismatch nothing is
    held, `held_paths=[]`, `last_error` = the error + " (changed while the node was down: …)", the process keeps
    running and its next respawn is refused. `policy_view()` adds `code_files` (every defined service's
    `approved_files` keys; the view cache is reset when they change) and `_service_code_patterns` adds them as exact
    paths, so gated identities cannot write a bound file while the definition exists (rule `exec_path`, "code of a
    managed service"; `test_services.test_restart_binds_script_argument` now expects that after the stop and frees the
    path on remove). Jobs: records store `approved_files` with `held_paths`; `_rehold` acquires against them (1.0.0
    records: current content); a mismatch -> not held, `held_paths=[]`, `rec["warning"]`, and `error` =
    "warning: approved file changed while the node was down: …" when no error was set. DESIGN §5.4 step 7, §5.6, T16
    (crash-restart residual closed for bound files) (v8.1).
X-S3. **cd in compound commands (finding 3, MEDIUM; `presets.py`, `hub.py`):** `presets.bindable_words(argv_or_command,
    shell=, os_name=) -> (words to bind, moved)`: like `candidate_words`, but after a word for which
    `presets.changes_cwd(w)` holds (basename, case-insensitive, `.exe` stripped, in `CWD_CHANGERS` = cd, pushd, popd,
    chdir, set-location, sl, push-location, pop-location; or an option matching `-C…`, `--directory[=…]`,
    `--chdir[=…]`, `-chdir=…`, `--work-tree[=…]`, `--git-dir[=…]`) every later *relative* candidate
    (`presets.relative_word`: not `~`, `/`, `\` or a drive path) goes to `moved` and is not bound.
    `presets.moved_items(moved, is_file)` names the path-like ones and those naming a file in the cwd as
    "<word> (cwd may change)" in the not-bound header (so `bash` and the `cd` target are left out). The node review
    and `Hub._review` share it; `Hub._split` now returns `(words, moved)`, `Hub._bind_words` returns
    `(words, cwd, moved)`, and the hub checks non-path-like moved words with one `fs.stat` each (`Hub._is_file`,
    request id suffix `.b900`). `candidate_words` is unchanged. DESIGN §5.6 (v8.1).
X-S4. **Exec paths (finding 4, MEDIUM; `policy.py`):** `DEFAULT_EXEC_PATHS` adds `~/.local/share/jupyter/**`,
    `~/Library/Jupyter/**`, `~/AppData/Roaming/jupyter/**`, `~/.bashrc.d/**`, `~/.zshrc.d/**`, `~/.zlogout`,
    `~/.oh-my-zsh/custom/**`, `~/.xsession`, `~/.pam_environment`, `~/.config/plasma-workspace/**`,
    `~/.kde*/Autostart/**`, `~/.kde*/env/**`, `~/.config/i3/**`, `~/.config/sway/**`, `~/.config/hypr/**`,
    `~/.Rprofile`, `~/.Renviron`, `~/.gdbinit`, `~/.emacs`, `~/.emacs.d/**`, `~/.config/emacs/**`, `~/.vim/**`,
    `~/.tmux.conf`, `~/.config/tmux/**`, `~/.wezterm.lua`, `~/.config/wezterm/**`, `~/.hgrc`, `**/.hg/hgrc`,
    `**/.yarnrc.yml`, `~/.config/direnv/**`, `~/.config/systemd/*.conf`, `~/.config/systemd/*.conf.d/**`.
    DESIGN §5.5, overview item 7 (v8.1). The allowlist idea (writable dot-paths for gated identities) is not taken:
    it would break ordinary project dotfiles (`.gitignore`, `.env.example`) for files tokens.
X-S5. **Job visibility (finding 5, LOW; `jobs.py`, `actions/jobs.py`):** `jobs.job_owner(identity)` (the stored
    `owner`, `display_name`), `jobs.sees_all_jobs(identity)`: true for kinds owner/system/node and for identities
    that are not `exec_gated` (§5.2 rule, or the hub/relay `exec_gated` flag). Otherwise job.list filters by owner
    (`JobManager.list(owner=)`) and job.status/job.logs/job.stop first call `JobManager.check_visible(job_id, owner)`
    -> `not_found` like an unknown id (`_rec(job_id, owner)`; the hint lists only the caller's recent jobs). Services
    stay shared (documented in SECURITY.md "Honest limits"). DESIGN §9.5 (v8.1).
X-S6. **Host header (finding 6, INFO; `httpserver.py`):** `httpserver.valid_host_header(v)`: `host[:1-5 digits]`
    with host a name/IPv4 (`[A-Za-z0-9_][A-Za-z0-9._-]*`) or a bracketed IPv6 literal (optional `%zone`); a request
    with a Host header that does not match -> 400 `bad_request` "invalid Host header" (hint "send Host as
    name[:port]") before `_client`/`_check_host`. DESIGN §6 (T7, route checks) (v8.1).
    Regression tests: `tests/test_sec_audit.py` (findings 1, 2, 3, 5), `test_policy.TestDenyWrite` (4),
    `test_httpserver.test_v81_strict_host_header` (6), `test_hub.TestHubFileBinding.test_v81_cd_unbinds_later_relative_words`
    (3, hub side), updated `test_shell` (cd header, review `cwd` key), `test_phase2_review` (`Hub._split` tuple),
    `test_services` (`code_files`, bound file stays protected while defined).

### External review, round 2 (independent reviewers, fresh containers)

R2-1. **Approval binding of `~` words (blocker; `presets.py`, `hub.py`):** words are bound as the path the program
    opens. `presets.bind_plan(argv_or_command, shell=, os_name=) -> (spellings, moved, unbound)` (new; `bindable_words`
    keeps its 2-tuple as the first two parts). POSIX shell strings are tokenized by `_posix_tokens` (per-character
    quoting: unquoted / inside "…" / literal), so only an unquoted, unescaped leading `~` (tilde prefix up to the first
    unquoted `/`) stays `~…` (expanded by the binding); a quoted/escaped one becomes the literal spelling `./~/…`
    (relative to the reviewed cwd, normally `absent`). argv elements are literal: a leading `~` is bound as `./~/…`
    **and** expanded (programs often expand it themselves); PowerShell/cmd words likewise. `KEY=~/x` words: bash
    expands only `NAME=~…`; `--opt=~/x` binds both. Variables: a leading `$HOME`-style variable of `policy.PATH_VARS`
    outside single quotes stays (PathGuard expands it; `bind_files` and `Hub._join` no longer join such a word to the
    cwd, which gave `<cwd>/home/…`); any other expanded `$`, or a literal word PathGuard would expand, is header item
    "<word> (variables; not bound)" when path-like. Services: the literal path is in the approval's files, so it is in
    `approved_files`/`code_files` (absent; created later -> respawn refused, gated writes refused). DESIGN §5.6, §8.2.
R2-2. **cd detection (major; `presets.text_may_change_cwd`):** for shell strings the decision is on the whole text:
    a cwd changer anywhere (`cd|chdir|pushd|popd|set-location|sl|push-location|pop-location` with delimiter-agnostic
    boundaries, so `cd/d`, `cd..`, `cd\sub`, `{cd`, `` `echo cd` ``, `C=cd`, `cd${IFS}` match; `SetLocation`,
    `SetCurrentDirectory`, `CurrentDirectory =`) or code the review cannot read (backtick, `$(`, eval, source, `. x`,
    iex/Invoke-Expression/Invoke-Command, a `$VAR`/`${…}` in command position) moves **every** relative word, before and
    after, to "(cwd may change)". The word-order rule (`changes_cwd`) stays for argv lists and for `-C`-style options in
    text. `Hub._split` returns the 3-tuple and drops words either reading moved. DESIGN §5.6.
R2-3. **Exec paths (major; `policy.DEFAULT_EXEC_PATHS`):** toolchain homes (`~/.cargo/**`, `~/.rustup/**`,
    `~/go/bin/**`, `~/.bun/**`, `~/.deno/**`, `~/.npm-global/**`, `~/.nvm/**`, `~/.volta/**`, `~/.local/share/pnpm/**`,
    `~/.pyenv/**`, `~/.rbenv/**`, `~/.sdkman/**`, `~/miniconda*/**`, `~/anaconda*/**`, `~/miniforge*/**`,
    `~/mambaforge*/**`, `~/.conda/**`; a `*` in these patterns also crosses `/`, so no `~/*conda*/**`), `~/.oh-my-zsh/**`, zsh/tcsh/csh/ksh/xonsh/nushell
    startup files, starship, terminal configs (alacritty, kitty, foot, ghostty, konsole), WM autostarts (openbox,
    labwc, river, bspwm/sxhkd, awesome, qtile, niri, herbstluftwm, xmonad, xfce4 xinitrc, lxsession), REPL rc files
    (julia, octave, ghci, irb, pry, psql, sqlite, lldb), `~/.mailcap`, VS Code extensions, `**/pyvenv.cfg`. The generic
    `~/**/bin/**` rule is not taken (project `bin/` folders). DESIGN §5.5.
R2-4. **Review binding uses the read check (minor; `presets.bind_files`):** words resolve with `resolve_path(cand)`
    (was `write=True`), so a service's bound file, an exec path or a held input is shown and bound; when the caller
    may not write it the file line says "…; agents cannot change it". The service.start review after an out-of-band
    edit now shows and binds the new content. DESIGN §5.6.
R2-5. **Hub binds the named path (minor; `Hub._bind_files`):** each `files` entry is keyed by the path as the command
    names it (cwd + word; `real` = the real path fs.stat returned, shown in the review lines); the node's
    `FileHolds.acquire` re-resolves it at consume, so a symlinked cwd (or word) retargeted after approval ends in
    `conflict`. Chosen over a hub-side cwd pin (no protocol field needed). DESIGN §8.2 step 4.
R2-6. **Env values that name files (minor; `presets.env_file_values`, `_env_words`):** BASH_ENV, ENV, PYTHONSTARTUP,
    LD_PRELOAD/DYLD_INSERT_LIBRARIES entries and NODE_OPTIONS `-r/--require/--import/--loader` targets are bound
    (relative -> `./x` against the reviewed cwd, so a missing one is `absent`; `~` both expanded and literal); a value
    with `$`/backtick is "env K=V (variables; not bound)". Node and hub (shell.run, job.start, service.define/start).
R2-7. Verification side effect (pkill of another agent's nodes): no code change.
R2-8. **Listen edits (major; `config._sync_local_node_listen`, `edit_hub_init`):** `init node --listen/--no-listen`
    on an existing config points client.json `this-pc`/`this-pc-owner` at the new URL or removes them (default moves to
    another connection), and moves the hub-on-this-machine's direct entry for the old URL (the CLI restarts that hub;
    with --no-listen it warns). `init hub --listen` moves `hub-local`. `cfg._notes` carries the printed lines.
    `cli._local_owner_conn`: node status / pause / resume use HTTP only with --connection/env or this-pc-owner
    (never the client default), else the lock and state files ("it does not listen, so this is read from its state
    files").
R2-9. **Uninstall (major; `install._stop_or_warn_services`, `services.running_offline`):** `--stop-services` also
    acts in the no-installation branch; without it a note names "N services and M jobs keep running; … use
    --stop-services". README uninstall blocks (Linux/macOS, Windows, pyz, English) use `--stop-services`.
R2-10. **Restart failure advice (major):** (a) `config.check_listen_raw(listen)` runs before init/edit writes (node and
    hub): plaintext on a non-loopback, non-tailnet host without TLS/allow_insecure_remote -> `forbidden` "refusing
    plaintext HTTP on H; nothing was changed". (b) `install.restart` records the log sizes (`logs/<kind>.log`,
    `logs/stdout.log`) before the control write; with no new holder it raises "the <kind> restarted but could not
    start: <reason>" when a start failure was logged since, and `_restart_daemon` prints it as "not applied: …".
R2-11. **service define (minor; CLI + `ServiceManager.define`):** state backoff/failed/exited -> stderr "it did not
    start: <last_error>", the last ≤ 8 output lines, the logs command, exit 1. A NEW definition whose manual start is
    refused with a port conflict is deleted ("…; the service was not saved"); an existing one stays.
R2-12. **--connection in hints (minor):** `_conn_flag(args)` is appended to define's printed follow-ups; `Out.error`
    extends a not_found "this is node '…'" hint with "other nodes are reached through the hub: --connection hub-local"
    when client.json has a hub connection (`_via_hub_hint`).
R2-13. **join --install failure (minor):** `cli._hive_cmd()` (pod wrapper, `python3 <pyz>`, `hive` or `python3 -m hive`,
    with `HIVE_HOME=` when set) in the fix hints; a start failure adds "the installed background service keeps
    retrying … to stop it instead: <cmd> service uninstall node".
R2-14. **Foreground restart (minor; `node.foreground_restart_ok`):** INVOCATION_ID alone no longer disables it.
R2-15. **pause/resume confirmation (minor; `node.pause` returns an id, `NodeRuntime._ack_paused`, `node.pause_acked`):**
    PAUSED holds `{"at","id"}`; the node writes `paused.ack` `{"paused","id","pid","at"}` when its watcher applies a
    change; `cli._wait_paused(…, data_dir=, paused=, nonce=)` accepts the ack or node.info. `hive node resume` waits
    ≤ 3 s the same way ("resumed: the running node did not confirm within 3 s …" otherwise).
R2-16. **Bare `hive init node|hub` on an existing config:** exit 0 "already set up at …; nothing to change"; README 4.2
    notes the line can be skipped after section 3.
R2-17. **service install with a hand-started daemon (minor):** `install.install` notes "a <kind> started by hand is
    running (pid N): stop it (Ctrl-C in its window, or hive <kind> stop) and the service takes over …" when the lock
    is held and nothing is installed.
R2-18. **init client token check (minor):** `CLIENT_TOKEN_RE = ^hv_[A-Za-z0-9_-]{32,256}$` before anything is saved
    (exit 2, "that does not look like a Hive token"); no URL probe.
R2-19. **Broken pipe (minor):** `cli.main` wraps `_main`; BrokenPipeError -> stdout to devnull, exit 0.
R2-20. **presets guide for CLI users (minor):** `hive guide presets` starts with `cli.PRESETS_CLI_LINE` (preset_args ->
    `--arg key=value`, model -> `--model`).
    Regression tests: `tests/test_ext_review2.py` (R2-1, 2, 4, 5, 6: presets unit, node end-to-end, service respawn,
    hub fake relay and hub with a real node), `tests/test_ext_review2_cli.py` (R2-8 … R2-20), `test_policy.TestDenyWrite`
    (R2-3), `test_services` port preflight (R2-11), `test_walkthrough.TestForegroundRestart` (R2-14), updated
    `test_sec_audit` (whole-text cd), `test_shell` (exec path bound and labelled), init no-op and token fixtures in
    `test_cli`/`test_walkthrough`/`test_wp6a_review`.

### QA report (independent QA session: flaky races, leaked processes and temp dirs)

QA-F1. **Job hold released after the final state was published (race):** `JobManager._finalize` set the final state and
    woke waiters before `_release_hold`/`_save`, so `wait_final` (and a yielded `shell.run`) could return while the
    approved file was still held ("[conflict] … in use by an approved command"). Now the hold is released first, then
    the final state is set and saved; `wait_final`/`wait` also wait while the job is in `_finalizing` (record saved by
    then). The `start_job` spawn-failure path releases before publishing "failed" too. Regression test (a blocked
    final-record write holds the window open): `test_jobs.TestJobManager.test_hold_released_before_final_state_is_published`.
QA-F2. **Queued autostart reported as settled "stopped":** after a node start, services queued for the staggered
    boot-time autostart stayed `state="stopped"` until spawned, and `ServiceManager._settled` treated that as final,
    so `service.status wait=N` returned "stopped" at once. `_settled` now treats stopped + desired running (not a
    manual stop) as pending while the manager runs (the autostart or the supervisor spawns it next). The e2e restart
    test registers the service pids before asserting. Regression test (spawn gated by an Event):
    `test_services.TestQAAutostartRaces.test_status_wait_covers_the_queued_autostart`.
QA-L1. **Autostart spawning after shutdown (orphaned shim + child per run of test_stop_on_exit):** `shutdown()` ran
    the stop-on-exit pass before setting `_stop` and joining the autostart thread. Now it sets `_stop`, joins the
    autostart thread and waits for supervisor spawns already handed to the pool, then runs the stop-on-exit pass;
    `_autostart` checks `_stop` before every spawn. Regression test (spawn gated until shutdown began):
    `test_services.TestQAAutostartRaces.test_shutdown_stops_a_service_the_autostart_was_spawning`.
QA-L2. **Temp dirs left per full run:** `helpers.make_runtime`/`start_node_server` and `wp5util.start_relay_node`
    register the homes they create (`helpers.temp_home`/`new_temp_home`); `rt.stop()` / `rt.test_stop()` delete them
    (`helpers.remove_on_stop`), an atexit hook removes any left. rmtree cleanups in `test_mcp_http` (class root),
    `test_util` (hivelock, hiveprune), `test_stubs` (hivepreset); `test_presets.test_local_model_rules` keeps its
    FakeRuntime referenced (a collected one removed its dir, then the test re-created it). Product: `NodeRuntime.stop()`
    joins the scheduler thread (≤ 5 s), and `paused.ack` is written with `mkdir=False`, so a periodic task never
    re-creates a deleted data dir after stop.
QA-R. **ResourceWarnings:** the test CONNECT proxy in `test_transport` now closes its upstream socket (5 "unclosed
    socket" warnings per run gone). One sporadic warning in `test_e2e_m2.test_e2e_1` (a closed socket collected in a
    client thread) did not reproduce alone and is left.
QA-M. `# pragma: no cover - Python < 3.12` on the `rmtree(onerror=)` branches of `actions/files.py` removed (they run
    on 3.9–3.11).
QA-S. **Stress verification after the QA fixes:** full suite 3x on python3 (3.13) and 3x on 3.9 (two runs in
    parallel), `tests/integration` 6x on each (two in parallel): 18/18 green, no failure or flake, no process and no
    /tmp entry left behind (21 orphaned "soe" shim pairs from runs before the QA-L1 fix remain from earlier sessions).
    Warnings found under load and fixed (test-side): the sporadic `test_e2e_m2` ResourceWarning (QA-R) was the SSE
    and status-only `http()` responses never closed: `m2util.sse_lines` closes its response, new `m2util.http_status`,
    the pyz/initialize responses closed; `test_m3_wsl` cleanup polled `reap_detached` once right after SIGKILL, so a
    keepalive Popen stayed in `proctree._detached` until exit ("subprocess N is still running"): it now waits until
    the keepalive pids are reaped; `test_walkthrough.TestForegroundNodeSurvivesConfigChange` used `pty.fork()` in the
    multi-threaded test process (DeprecationWarning on 3.12+, a possible child deadlock): `pty.openpty()` +
    `subprocess.Popen` (fork+exec in C). The WSL module ran 20x clean with `-W always::ResourceWarning`.

### Audit round 2

Independent approval/exec-path audit. Changes in `presets.py` (command scan, `bind_plan_ex`, `bind_files`),
`hub.py` (`_split`, `_bind_words`, `_bind_files` only), `policy.py` (`DEFAULT_EXEC_PATHS`); tests
`tests/test_audit2_binding.py`, `tests/test_policy.py` (exec-path lists), `tests/test_ext_review2.py` (expectations for
the named path and cmd PATHEXT); DESIGN §5.5, §5.6, overview items 6-7; SECURITY "Approvals" and "Exec paths" rows.

A2-1. **Script operands with plain names were not bound (HIGH):** `bind_files` skipped a missing word that was not
    path-like, so `bash run3` / `python3 foo2` with the script absent at review had `files: []` and an empty header;
    after approval the agent created `run3` and the payload ran. Owner decision: the operand of a known interpreter is
    always bound, missing → `bind:"absent"`; every other missing non-flag non-path word is listed in the header.
    New `presets._scan_commands` reads each simple command (POSIX lines split at `; & | ( )`/newlines by
    `_posix_scan`, the old `_posix_tokens` words with the structure kept, redirect targets and `2>&1` fd words set
    aside; PowerShell/cmd lines split outside quotes; argv as one command): `NAME=VALUE` prefixes and shell keywords,
    wrappers (`env`, `nice`, `timeout`, `sudo`, `doas`, `xargs`, `nohup`, `setsid`, `time`, `stdbuf`, `ionice`, `chrt`,
    `taskset`, `exec`, `command`, `builtin`, `caffeinate`, `chroot`, `busybox`, `wsl`, `uv|poetry|pipenv|pdm|conda|
    mamba|micromamba run`), then the interpreter (sh family, python/pypy/py, perl, ruby, node, deno, bun, Rscript, R,
    php, lua, awk, sed, tclsh, osascript, pwsh/powershell, cmd, `source`/`.`) with a per-interpreter option table.
    Its script operand is bound as `./name` (`_Roles.op`); `presets.bind_plan_ex` returns `(words, moved, unbound,
    quiet)` (`bind_plan` keeps its 3-tuple); `bind_files(..., quiet=)` lists a missing non-path word as
    "<word> (no file here, not bound)" unless quiet (`listable_missing`: not a command name/known option value/code,
    not a number, `NAME=VALUE` or text with whitespace). The hub uses the same plan (`Hub._split` returns the quiet set
    too; with the OS unknown a non-path word is quiet only when both readings say so) and the same listing.
    One step further (attacker view): options that take a value are skipped with it (`python -X dev s`, `-W x`,
    `-Xdev`, `bash -O opt`/`-o opt`/`+O opt`, `node --require x`/`--import`/`--env-file`, `perl -I dir`/`-Idir`,
    `ruby -I/-r`, `php -d/-c`, `deno --config`, `bun --preload`; clusters such as `-uB -X t`; `--`); code options mean
    no script (`-c`/`-e`/`-r`, `sh -s`, `command -v`, `sudo -l`); `sh -c`, `fish -c`, `env -S`, `cmd /c`, `pwsh
    -Command` text is planned again (depth ≤ 4); `pwsh -File`, `php -f`, `awk -f`, `sed -f`, `R -f`, `R CMD BATCH`
    values are the script; `python -m mod` binds `mod.py`, `mod/__init__.py`, `mod/__main__.py` (dotted: `pkg.py`,
    `pkg/__init__.py`, `pkg/sub.py`) quietly unless `-I`/`-P`; `lua -l m` binds `m.lua`; inline `BASH_ENV=x bash …`
    (and `env BASH_ENV=x …`) binds `x` like the `env` param; in cmd a non-builtin command word binds
    `name.com/.exe/.bat/.cmd` in the cwd (cmd searches it before PATH) and `call x` binds `x`; wrapper options that
    change the directory (`sudo -D`, `env -C`, `ruby -C`, `bun --cwd`, `wsl --cd`, `chroot`, `pwsh -wd`, `conda run
    --cwd`, `uv run --directory`) move every relative word; `pwsh -EncodedCommand` is a header item. Also: a `<`
    input redirect target is bound like an operand (`bash < run3`; heredoc delimiters and `<&3` are quiet); a
    python operand without a `.py`/`.pyz`/`.zip` suffix also binds `name/__main__.py` and a node operand `name.js`, `name.node`, `name/index.js`,
    `name/package.json` (quietly; a folder or extension-less name runs those); `env --`/`timeout --` keep reading
    assignments/positionals; PowerShell `Start-Process`/`saps` and cmd `start` are read like the command they start;
    `call :label` binds nothing. Quiet extras are
    planned last so they never push a named file over the 8-entry cap. Tests: `TestScriptOperand` (114 command
    forms and 10 argv forms bound, 28 not-overbroad cases, module files, cmd/PowerShell, cwd-changing options,
    quiet/listing, nesting bound), `TestNodeOperandBinding` (the repro end to end on a real node for `bash run3`, `python -X dev foo2`, an
    `env A=1 bash run4` argv and `timeout 30 bash -c 'bash run5'`: absent at review, created after approval →
    `conflict`, payload never ran; header lists `alpha (no file here, not bound)` but not `bash`, numbers, `FOO=1`;
    an existing operand behind `nice -n 5 bash -O extglob` still shown and swap-refused), `TestHubOperandBinding`.
    Limit (documented): `bash name`/`source name` may also search PATH; PATH folders inside rw roots are already exec
    paths for gated tokens.
A2-2. **Exec-path gaps (MEDIUM):** added `**/.git/modules/**`, `**/.git/info/attributes`, `~/.mavenrc`,
    `~/.gradle/init.gradle`, `~/.gradle/init.d/**`, `~/.local/share/dbus-1/services/**`, `**/.vscode/settings.json`,
    `**/.vscode/launch.json`, `~/.config/Code*/User/**`, `~/Library/Application Support/Code*/User/**`,
    `~/AppData/Roaming/Code*/User/**`; one step further: VSCodium and Cursor user dirs, `**/.git/config.worktree`,
    `**/.git/worktrees/*/config.worktree`, `~/.gradle/gradle.properties`, `**/.npmrc`. `test_policy` plants each for
    a `files` token (denied, operator allowed, reads fine); `test_v81_exec_paths_not_overbroad` gained neighbours that
    stay writable (`.vscode/extensions.json`, `.git/info/exclude`, `.git/objects/…`, `.git/worktrees/w1/HEAD`, a
    project `.gradle/init.gradle`, `~/.gradle/caches`, VS Code caches/logs, `dbus-1/other`, `npmrc.txt`, a plain
    `settings.json`); `~/proj/.vscode/settings.json` moved from that list to the denied list.
A2-3. **Node binding keyed the resolved path (LOW):** node `files` entries now carry the path as the command names
    it (`<cwd>/<word>`, `./` segments dropped by `presets._strip_dot_segments`) with `real` when it differs, like the
    hub (R2-5); `FileHolds.acquire` re-resolves it, so a link retargeted between approve and spawn is `conflict`. A
    second path to an already bound file (`bash a.sh; bash link.sh`) adds its own entry (same fingerprint, shown once)
    on the node and the hub. Test: `TestNodeOperandBinding.test_symlink_retargeted_after_approval`,
    `TestHubOperandBinding.test_hub_binds_second_path_to_same_file`.

#### Audit round 2 — verification findings

Verification of the round-2 fixes on a real node (cautious agent token + files token). Changes in `presets.py`
(`_posix_scan`, `_shell_spellings`, `_W`/`_Roles`, wrapper tables, `_scan_command`/`_scan_commands`, new
`_scan_text_runner`/`_scan_launcher`/`_uv_run`, `bind_plan_ex` via `_plan`, `moved_items`, `bind_files`), `hub.py`
(`_review`, `_stat_type`, `_bind_files` only), `policy.py` (`DEFAULT_EXEC_PATHS`, `Policy.live_deny_write`,
`EffectivePolicy._hop_keys`/`_bare_repo_shape`/`_deny_reason`); tests `tests/test_audit2_verify.py` (28: plan cases,
real-node repros absent at review → created after approval → `conflict`, payload never ran, hub cases, exec-path
cases), `tests/test_audit2_binding.py` and `tests/test_sec_audit.py` (expectations for `.pyc` module files and the
per-command cwd rule); DESIGN §5.5 exec-path list, §5.6 "Verification of audit round 2"; SECURITY "Exec paths" and
"Approvals" rows.

V-1. **Words dropped after a cwd change; eval/iex (blocker):** `eval` and PowerShell `iex`/`Invoke-Expression` now
    join their words and plan them again; in POSIX text the cwd is followed per simple command (`_Roles.cwd_segs`:
    cd/pushd/popd/chdir, eval, source/`.`, a program named by a variable or substitution), so words of commands before
    the first change stay bound (a loop/function/alias/trap starting before it keeps the old all-moved rule);
    `moved_items(..., quiet, exists)` lists a moved missing plain name as "(no file here, not bound)" on the node and
    the hub. `eval bash run3` → `./run3` absent → `conflict`; `cd .; flock /tmp/x bash run3` → "./run3 (cwd may
    change)"; `bash -c "$(cat run3)"` → `run3` bound (see V-2). PowerShell keeps the whole-text rule (its `$(…)` and
    script blocks run anywhere), so `iex "bash run3"` lists "./run3 (cwd may change)".
V-2. **Script text on stdin or in arguments (blocker):** here-strings (`_posix_scan` kind `herestr`) and
    `echo`/`printf` text piped into a shell with no script operand are planned again, `cat/head/tail x | sh` binds
    `x`; `$(…)`/`` `…` `` stay inside their word (unbindable), their text is planned again and `cat x` inside binds
    `x`; text runners `trap`, `alias`, `watch`, `su`/`runuser`/`sg -c`, `script -c` (clusters `-qc`), `flock -c`;
    `ssh host cmd` → "(remote command; not read or bound)"; any other argument with whitespace that starts with a known
    interpreter/wrapper/runner/eval/source/cd is planned again late and quietly (operands bound absent, no header
    noise: `git commit -m "bash is fun"`).
V-3. **`uv run --script`/`-s`/`--gui-script` (major):** the target is a Python script (op + entry files); `-m` uses
    the target as module; an unknown target is bound quietly with its entry files.
V-4. **Bytecode (major):** `**/__pycache__/**`, `**/*.pyc` added to `DEFAULT_EXEC_PATHS` (gated Hive writes only);
    `_module_files`/`_entry_files` bind `name.pyc`, `name/__init__.pyc`, `name/__main__.pyc` (`pkg/sub.pyc`) quietly.
V-5. **node folder operand (major):** `name/index.node` added to the node extras; a python/node operand that is a
    folder at review is named "<word>/ (folder; only its entry files are bound)" (`OP_MARK` in the quiet set).
V-6. **Exec paths through symbolic links (major):** gated writes are matched against the resolved key, the path as
    named (lexically normalized) and each link hop (`_hop_keys`); `Policy.live_deny_write` re-resolves pattern
    prefixes every 2 s, so links made by a dotfile manager after start are protected at both ends (repro: policy built
    first, then `~/.bashrc -> dotfiles/bashrc`, `~/.config/systemd -> ../dotfiles/config/systemd`; all four writes now
    `exec_path`). SECURITY documents dotfile managers and the residual (an unlinked second copy).
V-7. **gitfile (major):** `**/.git` added; a `config` beside a `HEAD` (or the reverse) is refused for gated writes
    (bare-repository shape). Residual documented: `net.download` checks patterns only; recommend
    `safe.bareRepository=explicit`.
V-8. **Windows PATHEXT / PowerShell (major, code reading):** cmd/`call`/`start`/`Start-Process` bind every default
    PATHEXT extension (plus the node's own on Windows); PowerShell `.\run3`/`& .\run3` bind `run3.ps1` and the
    PATHEXT names quietly. Absent entries got their own cap (`BIND_MAX_ABSENT` 48) so these never crowd out content
    binding.
V-9. **Unknown wrappers, quiet collisions (minor):** quiet is tracked per occurrence (`_W.quiet`/`vquiet`, louder
    spellings removed from the quiet set); `_NUMERIC_RE` needs a known unit (`1run`, `9x` are listed); csh/tcsh in
    the sh family; wrappers flock, strace, ltrace, nsenter, systemd-run, setpriv, firejail, unbuffer, valgrind,
    torsocks, proxychains, numactl, prlimit, xvfb-run, dbus-run-session, catchsegv, nocache; launchers torchrun,
    deepspeed, `accelerate launch`; `env -S` text joined with the remaining words.
V-10. **Over-broad unbinding (minor):** the dynamic/cwd decision for POSIX text uses the scanned command words
    (comments, quoted and plain arguments no longer unbind: `bash run3 # source`, `python3 train.py eval`);
    `source run3`/`. run3` bind their operand (the command after them moves); output redirect targets are neither
    bound nor listed, and a `flock` lock file is never listed.
    Test expectation updates: `test_sec_audit` (`cd sub && bash setup.sh` keeps `cd sub` bound; words before the first
    `cd` stay bound), `test_audit2_binding.test_python_module_files` (dotted module `.pyc` names).
    Note: the first commit of this work also swept in another session's uncommitted files present in the shared tree
    (`_regexworker.py`, `actions/files.py`, `fsops.py`, `httpserver.py`, `services.py`, `test_imports.py`).

### Audit round 3 (multi-machine)

Hub + direct node + relay pod + gateway audit. Changes in `hub.py` (links, relay routes, resolution only),
`gateway.py`, `httpserver.py`, `relay.py`; tests `tests/test_audit3_multi.py`; DESIGN §7.1, §7.2, §8.1, §8.3, §8.7,
T6, PROTOCOL §4, §5.1, §5.2, §6, SECURITY "Relay pods" row and limits.

A3-1. **Relay pod could take over a direct node's node_id (MEDIUM):** `DirectLink.node_id` was always "", so
    `_vet_node_id` could not refuse a pod registering with a direct node's id (readable through `node.info`) and
    `resolve` routed calls and gateway requests addressed by that id to the pod. Owner decision: `DirectLink` now
    learns its id from the authenticated probe — with each `/v1/actions` fetch (first probe, then every 5 min) it calls
    `node.info` over `/v1/call` with the hub token (`X-Hive-Agent: hub-probe`, only when the node implements it; a
    failed call keeps the known id) — so `_vet_node_id` refuses it like any other link's id. `Hub.resolve` matches
    names, then direct links' node_ids, then relay links', then aliases; an id that two links of the same kind report
    raises `not_found` "… is ambiguous", hint "address the node by name". When a probe learns an id a relay link
    already claimed (pod registered before the first probe), `Hub._direct_id` drops the claim from the live session
    and from `relay_ids.json` (log warning + audit "relay node_id claim dropped"). `gateway.audit_node` already
    tolerated resolution errors. End-to-end regression tests with a real hub, a real direct node and a `FakeRelay`
    pod: `TestRelayCannotTakeDirectNodeId` (register refused; `/v1/call` addressed by the id, upper-cased too, has
    `meta.node = desk` while the pod polls and gets no task; gateway by id → 403 for a `pod-a/*` app, desk's 404 for a
    `desk/*` app; a claim made before the probe is dropped and re-registering with it is refused; direct wins if both
    hold it; a pod keeps its own id), `TestAmbiguousNodeId`.
A3-2. **Relay session ids had 32 random bits (LOW):** `"s_" + new_id()` → `"s_" + secrets.token_urlsafe(24)`. Polls
    with an unknown or superseded session (409) now count per source IP in a separate `AuthThrottle` built from the
    hub `throttle` settings (the login throttle is cleared by every successful authentication, and a guesser holds a
    valid relay token): a blocked IP's polls get 429 `rate_limited` reason `auth_throttle` with `retry_after`
    (audit "relay session guesses blocked"); a successful register from the IP resets the count, so a real pod's
    409-then-register after a hub restart never accumulates. `RelayAgent` waits at least `retry_after` (≤ 600 s) on
    a 429. Tests: `TestRelaySession` (id format/uniqueness, block after 3 mismatches even with the right session,
    register resets, agent backoff).
A3-3. **Pod chose the Content-Type of hub gateway replies (LOW):** the relay path wrote the pod's `content_type`
    as is, `httpserver._write` filtered only CR/LF (so `\x0b`, NUL passed). New `gateway.safe_content_type` /
    `safe_headers`: only `application/json`, `text/event-stream`, `text/plain`, optionally `; charset=<token>`
    (normalized), anything else (other types, other parameters, control characters) → `application/json`;
    `Retry-After` only as digits; no other upstream header. Applied on the relay path, in `DirectLink.open_gateway`
    and in the gateway route for every backend (so a node's own gateway too). `httpserver._write`/`_write_stream`
    drop any header whose name is not a token or whose value has a control character (< 0x20 except tab, 0x7f); a
    dropped `Content-Type` falls back to the JSON default. Tests: `TestGatewayContentType`,
    `TestRelayGatewayContentType` (pod `text/html\x0bX-Evil: 1` → `application/json`, no injected header),
    `TestHeaderControlChars`.

Verification fixes (independent re-test of the round 3 fixes on loopback):

A3-2b. **Relay session throttle let one pod take every pod offline (MAJOR):** the bucket was per source IP, shared by
    every relay node, and consulted before the session — pod-b's 10 made-up sessions got pod-a's valid poll a 429
    (300 s), and pod-a's re-register did not unblock it (`AuthThrottle.ok` clears the history, never a block). Every
    pod behind one NAT egress or one `proxy:<peer>` bucket was exposed. Now `relay_poll` keys the throttle by
    `(node name, client_ip)` (a relay token polls only its own node's session), consults the block only when the
    poll's `session_id` does not match the live session (constant-time compare; a matching poll always proceeds,
    DESIGN §6 v4), and `relay_register` clears both the count and an active block for its `(node, ip)` key. Tests:
    `TestRelaySession.test_pods_sharing_one_ip_do_not_block_each_other`, `test_register_clears_an_active_block`,
    `test_session_mismatches_are_throttled_per_ip` (valid session proceeds while blocked). DESIGN §7.2, PROTOCOL §5.2.
A3-1b. **Direct node_ids were forgotten at every hub restart (MAJOR):** `DirectLink._node_id` started as "" on each
    start, so after a restart with the direct node offline a pod re-registering with its bound key and the direct
    node's id was accepted (and written to `relay_ids.json`), and calls and gateway requests addressed by that id
    reached the pod until the direct node came back and was probed. New `hub.DirectIds`
    (`<data>/state/direct_ids.json`, 0600, `{name: {node_id, url, tls_pin, learned_at}}`): `_learn_node_id` writes an
    entry when an authenticated `node.info` reports a different id; `Hub.__init__` loads the entries whose url and pin
    still match the config into `DirectLink._node_id` before the API server and the prober start, and runs
    `_direct_id` for each so an old `relay_ids.json` binding to that id is dropped. Remaining window (SECURITY.md):
    before the first-ever successful probe of a node (new hub, node added, url or pin changed, file deleted). Tests:
    `TestDirectIdSurvivesRestart` (restart with desk offline: register with desk's id refused, `/v1/call` by the id
    → desk's `unavailable`/`connect`, gateway by the id → 403 for a `pod-a/*` app, the polling pod gets no task;
    stale binding dropped at start; changed url or pin forgets the id). DESIGN §8.3, file table; PROTOCOL §5.1;
    SECURITY relay row and limits.
A3-4. **Gateway resolved the node twice (MINOR):** `HubGatewayBackend.authorize` resolved the node and matched the
    token's globs, then `forward` called `hub.resolve(node)` again with no glob check on the direct path, so a
    `node_id` moving between the two calls sent a `pod-a/*` app's request to desk. `authorize` now returns the
    canonical link name (the `GatewayBackend` protocol allows `Optional[str]`; `NodeGatewayBackend` still returns
    None), the route passes it to `forward` (and to the audit row), and `forward` looks the link up by name
    (`hub.links`) and re-checks `<link>/<service>` against the globs on both paths before `open_gateway` /
    `model.forward`. Tests: `TestGatewayResolvesOnce` (the probe moves the id from pod-a to desk inside authorize:
    the request still goes to pod-a, nothing reaches desk's audit; `forward` refuses a link the globs do not cover).
    DESIGN §8.7 step 1.

### Audit round 4 (DoS / local users)

Independent DoS / hub `/mcp` / local multi-user audit, with owner decisions. Findings 3, 6, 7 (this part): changes in
`httpserver.py`, `limits.py`, `node.py` (call slots), `hub.py` (relay result route, transport inflight cap),
`relay.py`, `config.py`; tests `tests/test_audit4_dos.py` (+ two updated cases in `tests/test_httpserver.py`); DESIGN
§6.1, §5.7 limits, §5.9, §7.3, §14 defaults; PROTOCOL §1, §5.3; SECURITY transport row and limits. Measured with
real `ApiServer`s on loopback (server in a child process, node defaults unless noted; scratch harness, not shipped).

A4-3. **Unauthenticated connection exhaustion / slowloris (HIGH):** 16 idle sockets from 127.0.0.1 (any local user,
    no token) got the owner's CLI/MCP a 503; 127.0.0.2+ aliases filled `max_connections`; through `proxy_port`
    (Funnel) all peers are 127.0.0.1, so 16 idle internet sockets locked out everyone; idle pre-auth sockets lived
    30 s. Now (`httpserver._ConnTable`, per the owner decisions): `max_connections` = pre-auth pool (`n // 2`) +
    authenticated pool (rest) with 4 slots (≤ half) only owner tokens accepted by the owner check may take (a
    connection moves pools with its first authenticated request; no room → 503 `busy`, `Retry-After: 1`); pre-auth
    buckets ≤ 16: one per remote IP, ONE for 127.0.0.0/8 + ::1, one for `proxy_port` and `trusted_proxies`; a
    newcomer to a full bucket (or full pool: largest bucket) evicts the oldest connection there that is still waiting
    for its head (socket-level shutdown, also for TLS sockets), else a static 503 with `Retry-After`; the pre-auth
    phase (TLS handshake + idle + head) has 5 s in total (`PREAUTH_DEADLINE`; from the accept, then from the end of
    each response on an unauthenticated keep-alive connection); authenticated keep-alive keeps 30 s. The accept
    thread no longer uses a semaphore; connection threads are started by `_BoundedServer.process_request` with their
    slot. A local owner Unix socket is future work (not needed with the reserve). Measured (owner GET /v1/echo while
    flooded; before → after): 16 idle from 127.0.0.1: connection reset (503) → 200 in 3 ms; 64 idle from
    127.0.0.2–5: 503 → 200; 16 and 64 idle on `proxy_port`: owner on the main port reset → 200, an agent through the
    proxy reset → 200; 200 idle mixed (4 loopback addresses + proxy): 503 → 200 (owner and agent); idle pre-auth
    socket closed after 30.0 s → 5.0 s. Capacity note: non-owner authenticated connections are now
    `n - n // 2 - 4` (node 28, hub 124; before: all 64 / 256 shared with idle sockets); a hub with very many relay
    pods (one long poll each) raises `listen.max_connections`. Residual: authenticated non-owner keep-alive
    connections can still fill the non-owner share of the authenticated pool (owners keep their reserve; idle
    authenticated sockets are not evicted, since a client may be sending on them).
A4-6. **Bodies buffered before per-identity limits (MEDIUM):** a hub buffered and parsed 48 MiB bodies with no global
    bound (40 concurrent ones: peak RSS 1580 MiB, 90 s wall, the pure-Python depth scan ~2 s per body holding the
    GIL). Now, after authentication and before the body is read: (a) a route `pre_body(headers, identity, length)`
    hook may refuse or answer unread; `/v1/relay/result` uses it: `RelayAgent` sends `X-Hive-Task` /
    `X-Hive-Session`, `RelayLink.expects_result` checks them and an unexpected result gets `{"accepted":false}` with
    the 30 MiB body never read; with the headers the body fields must match (400), without them (older pods) the
    body decides; (b) per non-owner identity (kind, name, token hash, agent) ≤ 32 requests with a body in flight
    (`ApiServer.inflight`, `KeyedLimit`; node: ≥ its per-identity call slots; hub: `max_inflight_per_agent` + 8) →
    429 `inflight`; (c) a global byte budget `listen.max_buffered_bytes` (node 128 MiB, hub 256 MiB, ≥ 2 ×
    `max_body_bytes`, a quarter reserved for owners; `limits.BodyBudget`) charged until the response is written, wait
    ≤ 2 s then 503 `busy` `Retry-After: 2`; both close the connection. (d) `json_depth_ok` runs at C speed: escape
    pairs dropped (backslash pairs, then backslash-quote, left to right as JSON pairs them), `translate` keeps only
    brackets/quotes, strings cut (find loop or one regex; an unterminated string runs to the end), `()`-removal passes, then
    `itertools.accumulate` over bracket runs; never True when the parse would nest deeper (fuzzed against the old
    loop). Measured, hub, 40 concurrent 48 MiB POSTs from two agent tokens + an owner 1 MiB POST: peak RSS 1580 MiB,
    90.7 s, all 40 buffered → 265 MiB, 9 s, 2–4 accepted at a time and the rest 503 `busy` before their bodies were
    read, the owner's POST 200 both times; depth scan per 48 MiB: string + structure 1.88 s → 0.07 s, numbers
    2.46 s → 0.05 s, adversarial depth-8 bracket soup 2.46 s → 1.27 s (its `json.loads` takes ~5.6 s). Residual:
    `json.loads` of a body that is mostly empty containers allocates many times the body size (the budget counts
    body bytes).
A4-7. **One token could take every node call slot (MEDIUM):** `ConcurrencyGate` now takes `owner_reserve` and
    `per_identity`; the node builds it with `policy.limits.owner_reserved_calls` (default 4, ≤ n − 1) and
    `policy.limits.max_calls_per_identity` (default and maximum: every non-reserved slot, i.e. 28 of 32); owner
    tokens may take any free slot; the key is (kind, name, token hash, agent), so agents behind one hub token are
    capped separately. A refusal where the identity cap (smaller than the shared slots) is what binds →
    `rate_limited` reason `inflight` `retry_after` 2; otherwise `unavailable` `busy` as before. Tests:
    `TestNodeCallSlots` (6 slots, 2 reserved, 2 per identity: a third call of one agent → inflight, a third agent →
    busy, the owner still runs 2), `TestLimits`.
    Tests (all in `tests/test_audit4_dos.py`): JSON depth equivalence/safety fuzz and speed (< 1.5 s for 48 MiB bodies
    that took ~2 s each), `_ConnTable` pools/eviction/owner reserve, a real server flooded from 127.0.0.1–3 and through
    `proxy_port` (owner and agent still 200, loopback bucket untouched by proxy sockets), the total pre-auth deadline
    against a dribbling head, owner reserve with the authenticated pool full, 429 / 503 answered before a 30 MiB body
    is sent and the owner exempt, `pre_body` short-circuit, a real hub refusing unexpected relay results unread and
    checking header/body agreement, `RelayAgent` sending the headers.

Findings 1, 2, 4, 5, 8 (the regex / local-user part): changes in `audit.py`, `secretstore.py` (masking),
`actions/system.py`, `jobs.py` (display only), `fsops.py` (`check_regex`), `actions/files.py` (fs.search),
`services.py` (`ready_pattern` matching), new module `_regexworker.py` (layout test and §3 count: 55 modules),
`util.py` (btime file), `hfhub.py`; package-wide regex sweep: `presets.py`, `policy.py`, `_download.py`. Tests
`tests/test_audit4_regex.py`, `tests/test_imports.py`; DESIGN §3, §4.2 (`process_create_time`), §5.8, §9.7
(fs.search, proc.list), §10 (`ready_pattern`), §11.5 (model.pull listing); SECURITY rows Audit / Agent regexes /
Local users and one limit. PROTOCOL unchanged (no wire change: fs.search `cut` now means "past the first 1 KiB",
`reason` may be `"regex_worker_failed"`). Timings on this container (CPython 3.13).

A4-1. **Quadratic redaction regexes (CRITICAL):** the URL-userinfo rule (`scheme*://`), the `--flag` rule
    (`X*KEY X*`), the `NAME=` rule and the mysql `-p` rule (lazy scan to end of line from every client name) were
    quadratic, and `_short()` redacted the whole string before cutting it. Now: flag and NAME words are consumed
    atomically (`(?=lazy…secret word)(?=(word*))\1`: Python 3.9 has no atomic groups), URL userinfo is matched from
    `://` on (whatever precedes it is kept), the mysql rule is a small linear scanner (the first `-p` with a value
    after each client name within its `\n ; | &` segment; a `\n` may be the blank before `-p`, as before);
    differential fuzz against the old rules: 0 differences in 2 × 200 000 random command lines. `audit.redact_head(s,
    limit)` redacts only a 2 × limit + 64 window (secret-value mask 16 KiB further, so a value straddling the cut is
    masked whole; the word cut by the window is dropped, never half-shown); `_short`, proc.list and job display use
    it. `SecretStore.mask`: `str.find` per value + union of overlapping occurrences (the regex alternation re-compared
    a long value at every position). Before → after: `redact_params` with a 200 KB value `'a1'*` 13.9 s → 0.4 ms,
    `'KEY'*` 105.6 s → 0.3 ms; whole-text `redact_text` on 1 MB adversarial inputs (43 shapes) ≤ ~0.3 s, linear
    (20 KB `'--key'*` was 2.2 s); `mask` of 1 MB `'A'` with a value `'A'*16383+'B'` 49.9 s → 0.03 s.
A4-2. **proc.list / job display froze on a huge argv (HIGH):** `_cmd_text` and `display_command` redacted the
    full command before cutting it to 300 / 200 chars; now `redact_head`. The proc.list `filter` (matched against
    the redacted command line so `total` is no secret oracle) now sees the name + the first 4 KiB
    (`system.FILTER_MAX`). A 1 MB argv: ~100 s (`'KEY'*` shape) → < 1 ms per process.
A4-4. **Polynomial fs.search regexes (HIGH):** (a) regex matching runs only in the new `_regexworker` child:
    `python -I -c WORKER_SOURCE` (stdlib only, never imports hive, no cwd/PYTHONPATH/user site), 4-byte length +
    ASCII JSON frames over pipes, header `{pattern, flags, alarm_s}` then `{"lines": […]}` batches (≤ 1 MiB / 8192
    lines) → `{"hits": [i…]}`; a watchdog thread kills it at the call deadline or on cancel (POSIX: plus
    `signal.alarm` in the child, default action, so an orphan dies too); fs.search returns partial results
    `truncated:true, reason:"timeout"` (`"regex_worker_failed"` for any other death); the pattern is still compiled
    in the node by `check_regex` (validation only, never matched); literal search unchanged. (b) `check_regex` also
    refuses adjacent unbounded quantifiers over overlapping characters (only optional items between: `A*A*c`,
    `\w+\d+`, `.*\s*x`, `(?i)a*A*b`) and chains of three unbounded quantifiers joined by atoms all of them can consume
    (`.*a.*a.*c`), analysed on the `re` parser tree (`re._parser`, `sre_parse` on 3.9/3.10) with a sample alphabet;
    common patterns (`.*foo.*`, `a.*b.*c`, `\w+\s*=\s*\w+`, `def \w+\(.*\)`) still pass; `ignore_case` is passed as
    flags. (c) Regex mode matches the first 1 KiB of a line (was 2 KiB; catalog `cut` text updated). (d) Service
    `ready_pattern` is matched per new log line (first 1 KiB) in the same worker with a 5 s limit, and only when the
    log grew since the last check (was `rx.search` over up to 1 MiB in the node). Audit example `A*A*A*A*c` on a
    250-char line: 19.9 s frozen → refused (`bad_request`); with the check bypassed (test) the child is killed at the
    deadline and a ticker thread in the node keeps running throughout; a worker round trip costs ~15 ms.
    Note: the code of this item landed inside the concurrent commit 0ccfb7e (another agent's commit picked up the
    files); its tests are in 7c82465.
A4-5. **btime FIFO at a predictable shared path (HIGH):** `_stable_btime` now writes only to private per-user
    directories (`$XDG_RUNTIME_DIR` when it is ours and not group/other-writable, else `<passwd home>/.hive/run` —
    the account's home, not `$HOME`, so a job env cannot split it — else `$HIVE_HOME/run`; created 0700), reads with
    `O_NONBLOCK | O_NOFOLLOW | O_NOCTTY` and accepts only a regular file owned by this uid with no group/other bits;
    files of earlier boots are pruned. The pre-round-4 locations (`/dev/shm`, temp dir) are read once, with the same
    checks, so a node upgraded mid-boot keeps the value its older processes used (then copied to the private dir);
    they are never written. Before: a FIFO planted at `/dev/shm/hive-btime-<uid>-<boot_id>` blocked every Hive
    process at startup; after: ignored (test with FIFOs in the shared and in the private dir).
A4-8. **Unbounded model listings (LOW/MEDIUM):** `hfhub.MAX_ENTRIES = 200_000` listing entries in all (every HF page
    counted before parsing its files; one ModelScope reply) → `too_large` `details.max_entries`, hint naming
    `net.download` (deviation from the suggested "use include" hint: include/exclude only filter a listing that was
    already fetched, so they cannot shrink it); only listings of ≤ 20 000 files are cached (`CACHE_MAX_ENTRIES`), so
    a very large listing that timed out is listed again rather than held.
A4-sweep. **Other regexes on attacker-controlled text** (every module-level pattern of the package run against
    adversarial inputs: single/paired characters and the pattern's own literals, plus the inline `re.*` calls
    reviewed): `presets._DYNAMIC_TEXT_RE` (approval review of a command: `\s*` after a separator rescanned every
    following newline; 40 k newlines 11 s → 1 MB 0.03 s, `[^\S\n]*`, 0 differences in 300 000 fuzz cases);
    `policy._HIVE_CMD_RE` in `check_kill` (a local process whose cmdline repeats "hive": 100 KB 18–29 s → 1 MB
    0.015 s via `_is_hive_cmd`: first start token per line, then a role word after it; 0 fuzz differences);
    `hfhub.LINK_NEXT_RE` (a mirror's `Link` header of 64 KiB of `<`: 16.9 s → < 1 ms, `<([^<>]{1,8192})>`);
    `_download` `filename*=` (65 KB header 1.0 s → 7 ms; that one ran in the `_download` child). Not changed: owner
    `command_allow`/`command_deny` regexes (owner-written; SECURITY limit added), local tool output parsers
    (doctor, sysinfo, gpu), catalog schema patterns (checked: linear).
    Tests (`tests/test_audit4_regex.py`, 24): every redaction entry point on 1 MB (whole-text `redact_text` on
    256 KB) adversarial inputs with generous bounds (0.5–1 s; the old code took minutes to hours), redact_head
    cut/straddle cases and rule goldens, mask on 1 MB and union semantics, check_regex reject/accept tables, worker
    matching / kill at deadline with a responsive parent / cancel, fs.search never matching in-process (patched
    `re.compile`) and killing a bypassed `A*A*A*A*c`, `ready_pattern` via the worker, btime FIFOs and private dirs,
    listing caps and cache limit, the swept regexes on 1 MB.

Verification of round 4 (live re-run of every attack on loopback: hub, node as another user, relay pod, a Python 3.9
node, attacker `nobody`; all earlier items held). New findings fixed here: changes in `fsops.py`, `actions/files.py`,
`audit.py`, `httpserver.py`, `mcp.py`, `gateway.py`, `hub.py` (relay result parse cap only), `hfhub.py`, `policy.py`
(dead regex removed); tests `tests/test_verify4_dos.py`, `TestEditDoSVerification4` in `tests/test_fsops.py`,
`TestBoundedRecordsVerification4` in `tests/test_audit.py` (+ a racy budget assertion in `tests/test_audit4_dos.py`
now waits for the server thread); DESIGN §6.1 (backlog, JSON), §9.7 (fs.edit, fs.search, check_regex); PROTOCOL §1,
§5.3; SECURITY Audit / Agent regexes rows and the body limit. Timings: this container, CPython 3.13 / 3.9.

V4-1. **fs.edit `closest` was cubic (BLOCKER):** `closest_window` ran difflib `ratio()` (autojunk off) per window, no
    deadline: 400 / 800 `old_text` lines 4.6 / 14.9 s per call, a 1 MB `old_text` burned a node thread for 26+ min.
    Now bounded: the first 200 lines / 8 KiB of `old_text`; windows ranked by how many of their whitespace-stripped
    lines occur in `old_text` (prefix sums over the file, linear), ties broken by a character-count similarity
    (`quick_ratio` from two `Counter`s) within a 4 Mi-char budget; the call deadline is checked between the steps and
    every 64 windows → `details.closest: null`. A long `old_text` whose first/last 16 chars do not occur is not
    searched at all. After: 16 KB file / 1.6 KB `old_text` 0.08 s (3.9: 0.15 s), 1 MB `old_text` 0.12 s, a 4 MB file
    with 1 MB `old_text` 0.35 s, 16 MB of code with a typo'd function 0.6 s. Lines are now split at C speed
    (`re.split`), also for the success snippet, which splits only the edited region (`snippet(offset=)`).
V4-2. **Python 3.9 `str.find` is O(n·m) (BLOCKER on 3.9):** `'A'*k+'B'+'A'*k` needles held the GIL for 6–30 s
    (fs.edit) and 1.6 s per file (fs.search literal, 500-char needle). `fsops.LiteralSearch`: on Python < 3.10 a
    needle of ≥ 32 chars is searched with a pure-literal pattern compiled by `sre_compile` directly (not through the
    `re` cache, which would keep 512 large needles); sre turns it into a literal prefix with a KMP overlap table, so
    find / count / finditer / sub are linear (verified: 1 MB needle in 16 MB, 0.03 s search; compiling costs ~1 µs
    per needle char in pure Python, which releases the GIL). 3.10+ keep the str methods (two-way search). After
    (3.9): 2 MB file / 100 KB `old_text` 0.14 s found, 0.13 s not found; fs.search literal 500-char needle on a
    16 MiB line 0.02 s.
V4-3. **Match line numbers were matches × file size (MAJOR):** `_line_of` counted from offset 0 for every match (300 k
    matches: 30 s). `apply_edit_at` counts newlines incrementally for the first 100 matches only (conflict
    `details.lines` and the replace_all result list hold ≤ 100 lines) and the total with `count`. 16 MB file of
    `a\n`, `old_text` `a`: conflict 0.03 s, replace_all 0.04 s (+ snippet 0.003 s).
V4-4. **Audit records copied params containers whole (MAJOR):** a 48 MiB body of empty lists wrote a 50 MB line.
    `AuditLog.record` now redacts with a budget (`redact_params(..., bounded=True)`): ≤ 32 items/keys per container
    (argv 128), ≤ 2000 values, depth ≤ 24, the rest summarized (`"…[+N items]"`, `{"…": "[N keys]"}`); body keys
    holding containers of more than 10 000 values get `{"items": N, "sha256": null}` instead of a canonical-JSON
    hash; a record over 64 KiB has its `params` (then `extra`) replaced by `{"…": "[record cut: N bytes]", "keys"}`,
    else only ts/id/kind/action/ok/code remain. A 16M-element params list: 0.2 ms, a 368-byte record. Unbounded
    `redact_params` (approval review display, node fallback) is unchanged.
V4-5 / V4-6. **JSON parse amplification and GIL-holding pre-scan (MAJOR):** `json.loads` of 48 MiB `[],` allocates
    ~1.1 GB (hub peak 13.2 GB from 40 bodies), and the depth scan's whole-body `re.sub` / `translate` held the GIL
    1.3–1.8 s on `"[`-style bodies. `httpserver._json_bracket_seq` now builds the outside-string bracket sequence in
    1 MiB slices (string state and a trailing lone backslash carried across; fuzzed against the reference loop with
    3-byte slices) and stops once more than the container cap opened; `json_body_problem` / `check_json_body`:
    `Request.json(max_containers=262144)` (`MAX_JSON_CONTAINERS`; relay results via `_relay_body` 1 048 576, the
    legacy path without `X-Hive-Task` included), `/mcp`, and gateway bodies → 413 `too_large` before `json.loads`.
    Brackets inside strings never count (a file's text in `content` passes). After, 48 MiB bodies: `[],` refused in
    0.05 s, `"[` / `"[]` / `[]"` in 0.1–0.15 s with a longest GIL gap ≤ 0.06 s (was 1.3–1.8 s), plain string body
    0.05 s. Residual: within the cap, numbers and short strings still expand a few times when parsed.
V4-7. **Bounded quantifiers bypassed the regex rules (minor):** `{m,n}` with n − m > 16 (`LARGE_REPEAT_RANGE`) now
    counts as unbounded in `_scan_regex` (unbounded count) and `_overlap_problem` (adjacent / chained overlap), and a
    group quantified with an upper bound > 1 may not contain an unbounded quantifier (this also closes `(?:a+){10}c`,
    which passed before: the group-repeat rule only looked at counts > 10). All six reported patterns are refused;
    `\d{1,3}`, `(\d{1,3}\.){3}`, `[0-9a-f]{40}`, `x{0,1000}y`, `(?:ab){0,30}c` still pass. Regex-mode fs.search
    stops after 120 s (`REGEX_SEARCH_MAX_S`, partial results, reason `timeout`).
V4-8. **Listen backlog (minor):** `request_queue_size` = 4 × `max_connections`, between 64 and 1024
    (`httpserver.listen_backlog`; node 256, hub 1024; the kernel still caps it at `net.core.somaxconn`).
V4-9. **presets.bind_plan on 100 KB commands (minor):** not changed here (approval-binding area, owned by the
    binding work in progress).
V4-10. **Dead quadratic `policy._HIVE_CMD_RE` (minor):** removed; the test keeps it as the oracle for `_is_hive_cmd`
    (4000 random lines, 0 differences).
V4-11. **hfhub listing transient memory (minor):** each HF page (and the ModelScope reply) is pre-counted with
    `json_body_problem` before `json.loads`: more than 4 arrays/objects per remaining entry (`CONTAINERS_PER_ENTRY`)
    → `too_large` like the entry cap (16 MiB of `{}`: refused unparsed instead of +360 MiB); entries whose path is
    longer than 4096 chars are skipped.

### Final acceptance (independent session)

The independent acceptance run passed 1527 tests on 3.9–3.13 with one environment-dependent failure and confirmed
every earlier beginner problem fixed; the remaining items, fixed with the owner's decisions:

FA-A. **Tests on the default ports (Part A):** `tests/test_cli.py::TestNodeCommands` ran `hive init node` with the
    default listener 127.0.0.1:7701; `node status` / `node restart` then reached a developer's own node there (401 →
    exit 3). `tests.test_cli.run` now passes `--listen 127.0.0.1:<free port>` to every `hive init node|hub` that
    creates (or `--force` rewrites) a config without choosing its listener (`free_port_init`); the one test that
    asserts the printed default URL passes `--listen 127.0.0.1:7700` explicitly (nothing connects). Audit: the whole
    suite (clean HEAD copy) ran with a real node on 7701 and a real hub on 7700; their request logs, matched to
    per-test start/end times, showed contacts only from those three `TestNodeCommands` tests (`POST /v1/call` 401). All
    other 7700/7701 occurrences are URL strings in parsing, rendering, join-code and config tests (no socket), or
    integration tests that already set a free port. `TestNoDefaultPorts` checks the helper and the written configs.
FA-B1. **`examples/stub_openai.py`:** stdlib OpenAI-compatible stub (`/health`, `/v1/models`,
    `/v1/chat/completions` incl. `stream` SSE with optional usage chunk, `/v1/completions`; `PORT [--model] [--host]`,
    127.0.0.1 by default, 1 MiB body cap). README 6.1 copies it with `hive push desk hive/examples/stub_openai.py
    '~/hive-test/stub_openai.py'`, defines it with `--cwd '~/hive-test'` (quoted: `~` is the node's home) and checks it
    with `hive call model.chat service=stub prompt=你好 --node desk` (verified against a real node). Tests run it as a
    subprocess (`TestStubOpenAI`).
FA-B2. **`--node X` on the hub machine (`cli._auto_hub_connection`):** without `--connection`, HIVE_URL or
    HIVE_CONNECTION, when the connection the command would use is this computer's node (`this-pc`/`this-pc-owner`)
    and X is neither that node (its config name) nor self/local/./-, the command uses client.json's hub connection
    (`hub-local`, else a non-local default connection, else a name containing "hub") and says so on stderr
    ("note: 'desk' is not this computer's node ('blk'): using the hub connection 'hub-local' …"); printed follow-up
    commands carry `--connection`. Applies to `call`, `actions`, `tools export`, `service define` (`--node`) and to
    `sh`, `push`, `pull`, `logs` (NODE argument). README 6.1 explains it and shows `--connection hub-local`.
FA-B3. **`hive node token set NAME --cautious|--no-cautious`** (agent tokens; `require_approval` =
    `CAUTIOUS_ACTIONS` or removed; the hash stays, so agent configs are unchanged; restarts a running node) and the
    hub twin **`hive hub set-agent NAME --cautious|--no-cautious`**. `hive doctor` prints the exact command
    (`hive node token set <name> --cautious`, `hive hub set-agent <name> --cautious, or narrow its nodes`; T25 names
    `hive hub set-agent NAME --cautious`); `init hub --with-local-node` names `hive node token set this-pc --cautious`.
FA-B4. **`hive mcp-config --client X --remove`:** claude-desktop / cursor (`~/.cursor/mcp.json`): deletes only the
    `"hive"` server (other servers and keys kept, `.bak` copy), "nothing to remove" when absent, needs no Hive config;
    claude-code: prints `claude mcp remove hive` (and the `-s user` form); generic: usage error. README 11 runs it
    first in the uninstall steps and says how to remove the entry by hand.
FA-B5a. **Unknown action through the hub (`hub._unknown_action`):** "upgrade the hub" only when the target node
    implements the action (a newer node); otherwise `unknown action 'x'` with the closest names (catalog + the node's
    actions, `details.suggestions`) and "list the actions: hive actions", plus both versions when they differ.
FA-B5b. **Approval review and echo:** words after `echo` / `printf` / `Write-Output` / `Write-Host` without `.`, `/`
    or `\` are quiet (`presets._PRINT_ONLY`), so they are no longer listed as "(no file here, not bound)"; path-like
    words, the same word used elsewhere as a file, interpreter operands and text piped into a shell keep the rules.
FA-B5c. **Relay goodbye (`POST /v1/relay/unregister`, PROTOCOL 5.3a, DESIGN 7.3a):** `RelayAgent.check_disabled`
    (poll loop and the node's 1 s PAUSED watcher) sends it when `relay.disabled` appears (reason `panic` when panic
    wrote it, else `paused`); `run_node` stops the agent with `goodbye=True` unless the exit code is 75 (restart).
    `RelayLink.unregister` ends the matching session: offline at once, queued calls fail `offline` (not executed),
    delivered ones `offline` with `maybe_executed` = mutating, the hint names the reason (`hive node resume`); a stale
    session is ignored; a register clears it.
FA-B5d. **`gateway url --token-file`:** the "no token yet?" line is printed only without `--token-file`.
FA-B5e. **Direct nodes just added:** NodeSummary `checking` (true until the hub's first probe of that link ends);
    `hub status` / `hive nodes` show `checking` instead of `offline`. The prober re-probes a direct node that has not
    been reached since the hub started after 2 and 4 s, then every 5 s for the hub's first 10 minutes
    (`FAST_PROBE_MAX`, `FAST_PROBE_WINDOW`), so a node that accepts the hub token later (`hive node token add … --kind
    hub` on that machine) is online within seconds instead of up to 30 s. README 4.2 explains the states.
FA-B5f. **"applied by the watchdog":** `install.supervisor(kind)` (watchdog pid alive, `deploy/runpod/start-hive.sh`
    loop — it now writes `node/run/start-hive.pid` —, the Windows launcher, or an active/activating systemd unit);
    `cli._restart_daemon` prints "the node is not running right now; applied by the watchdog, which starts it again
    within a few seconds" instead of "will apply on next start" when one is looping (e.g. `init node --no-listen`
    after a port conflict on a pod).
FA-B5g. **`hub restart-node --outdated` without auto-update:** the node reports why (`node.info modes.auto_update_off`
    / NodeSummary `auto_update_off`: `role:<role>`, `deny_actions`, `not_pod` (no `HIVE_WATCHDOG=1`), `off`); the CLI
    prints the reason and the remedy (role narrowed: `hive init node --relay-role operator --auto-update`; not a pod:
    upgrade by hand with the commands; older nodes without the field: both possible reasons). New `hive init node
    --auto-update / --no-auto-update` (on needs a relay with role operator and no deny_actions). README 11 explains.
