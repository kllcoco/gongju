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
