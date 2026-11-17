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
    - `service.status.resources.gpu_memory_mb` for containers (nvidia pids vs `docker top`) is not implemented
      (null unless the service's own pids appear in `gpu.process_memory()`).
    - The wsl.exe refusal hint does not name a `wsl-host:<node>` tagged node (the node does not know the hub's
      node list).
    - The §22.2 WP2 v8 item "the relay-role gateway hint" belongs to the gateway/model.forward path (§8.7, WP3/WP5);
      no WP2 module produces that error.
