# 蜂巢 Hive — Security

Hive lets AI agents run commands, change files and start services on your computers. **This is remote code
execution by design.** Read this page once before you give an agent a token. The binding design is
`docs/DESIGN.md` §5; this page summarises it for owners.

## What protects you

| Layer | What it does |
|---|---|
| Tokens | Every HTTP request needs a 256-bit `hv_…` Bearer token, loopback included. Only SHA-256 hashes are stored; every stored hash is compared in constant time; failed logins are throttled per IP (10 / 60 s → 5 min block). |
| Classes | Each token has a class set: `readonly` = read, `files` = read + write, `operator` = read, write, exec, net, persist. `deny_actions` narrows further. An action needs its risk class plus any extra class (`net.download`/`model.pull` also need `write`, `service.define` also needs `exec`). |
| Node policy | `allow_actions`/`deny_actions`, `read_only`, `exec.enabled`, `exec.mode` (`open` or `allowlist`), filesystem roots (`rw`/`ro`), deny lists, network allow lists, size and time limits. The node is the final enforcement point for every route (direct, hub, relay). |
| PathGuard | Paths are expanded (only `~` and a fixed list of non-secret variables), checked lexically against the roots **before any filesystem call**, resolved with `realpath`, checked again, then matched against deny lists. Hive's own files (`$HIVE_HOME`, the data dir, any `.hive` folder, config and token files, TLS keys named in configs, `/proc/*/environ`) are never reachable. Hive's code, the Python interpreter and every `sys.path` folder are never writable. |
| Exec paths | Identities without `exec` (and approval-gated or hub-gated ones, and **every** identity on a node whose policy contains execution) cannot write shell profiles, autostart folders, Git hooks/config, `.pth` files, site-packages, venv `bin`/`Scripts`, PATH folders inside writable roots, or a managed service's code folders and the files its approval bound. The list covers common autoruns (v8.1 adds Jupyter kernel specs, `~/.bashrc.d`, KDE/i3/sway/Hyprland session scripts, tmux/emacs/vim/WezTerm configs, `.Rprofile`, `.gdbinit`, Mercurial and Yarn configs, `~/.pam_environment`), not every program that reads a config at start: add your own (`policy.fs.deny_write`). |
| Approvals | Agent tokens with `require_approval` (the `cautious` preset) must get your OK for shell commands, service starts, deletes, moves and kills. You decide with the owner token **from the machine itself** (`hive approvals approve ap_…`); proxied requests never count as local. An approval binds token name + token hash + node + action + exact parameters, and the script files the command names (changed files → `conflict`), resolved in the working folder the agent asked for (shown as given, even when it does not exist yet; a folder that later resolves elsewhere → `conflict`). Files named after a `cd`/`-C` are not bound and are flagged "cwd may change". A managed service's crash restart, autostart and re-adoption after a node restart check the approved files again and refuse to run changed ones ("approved file changed; approve a start again"). It is single use and expires after 10 minutes. `--for 30m` grants time-boxed repeats. Pausing the node revokes its grants. |
| Network | Outbound HTTP is limited to allowed categories (default loopback + public; LAN, link-local and cloud metadata addresses are refused). Gated identities cannot POST to local services except known OpenAI endpoints, cannot send secrets with write methods, and their write requests through a proxy are pinned to the locally vetted IP. |
| Secrets | Stored on the node (`secrets.json`, 0600), referenced by name, bound to hosts, sent only over verified TLS (or to loopback/private targets), never written to audit, logs or results (values are replaced by `«secret:NAME»`). |
| Transport | Loopback bind by default; any other bind needs TLS or a Tailscale address (WireGuard), and at least one token. Headers within 10 s, bodies read only after authentication, size limits before reading, ≤ 16 unauthenticated connections per IP, `Origin` refused (except the gateway's CORS routes), `Host` checked per connection (DNS rebinding) and parsed strictly as `host[:port]` (anything else → 400). |
| Audit | Every call, denial, approval decision, auth failure and lifecycle event goes to `<data>/audit/audit-YYYYMMDD.jsonl` (redacted: secret-looking keys, bodies hashed, env/headers by name only). `audit.required: true` refuses calls that cannot be audited. |
| Kill switch | `hive node pause` (all non-read calls refused within 1 s, nothing spawned), `hive node panic` (pause + relay off + stop every job, service and labelled container, also with the daemon down). |

## Honest limits (residual risks)

- **Shell access is full user access.** `shell.run`, `job.start` and services run with all rights of the node's OS
  user. Roots and command regexes are guardrails against accidents and naive prompt injection, **not a sandbox**.
  Real containment: tokens without `exec`/`persist`, `exec.mode: "allowlist"`, `read_only`, or a dedicated
  low-privilege OS user / container.
- An exec-capable agent can read `secrets.json` through the shell as the same OS user and can print any secret it is
  allowed to inject. Masking is a speed bump.
- An approval covers the command, its env/stdin and the files it names, not other files the program reads (imports,
  includes, sourced scripts). Keep inputs that must not change in a read-only root (`--root PATH:ro`). `cautious`
  stops unapproved code execution, not data loss: overwrites are not gated.
- **Jobs are private, services are shared.** A cautious, `readonly` or `files` token (and a gated hub/relay agent)
  sees, reads and stops only the jobs it started; the owner and tokens with an ungated shell see all jobs. Managed
  services are the node's infrastructure: every token that may call `service.list`/`service.logs`/`service.stop` sees
  and controls every service, including services other agents defined, and service logs are readable by any token
  with `read`. Do not let a service print secrets you would not show a readonly token, and give agents that must not
  stop your model servers `deny_actions: ["service.stop", "service.restart"]`.
- GET/HEAD to loopback stays open to every token with `net`: local admin APIs that serve secrets on GET (Clash
  controller `127.0.0.1:9090`, Docker TCP `127.0.0.1:2375`) are readable. Close them with `net.http_deny`
  `"HOST:PORT"` entries.
- Through an HTTP proxy the proxy resolves names, so DNS pinning cannot hold for reads (write requests of gated tokens
  are pinned). `network.strict_dns: true` restores pinning.
- A compromised hub can do on each node what that node's `kind:hub` token (or `relay.role` on pods) allows, and can
  skip its own approvals. An exec-capable agent on the hub's machine equals the hub.
- Docker group membership is root on the host; the docker preset's checks are convenience only.
- WSL interop and automount let Linux processes reach the Windows profile; disable them if the WSL node must be a
  boundary.
- Audit files can be rewritten by a same-user shell (no hash chain yet).

## Hardening guide

1. Prefer Tailscale/WireGuard; bind to the tailnet IP. Never port-forward without TLS + pin.
2. Give untrusted or browsing agents `readonly` or `files` tokens; `operator` only to agents whose inputs you control.
3. For real isolation run the node as a dedicated OS user whose home holds the models; make the config owned by
   another user and read-only to it. On RunPod the pod is the boundary.
4. `exec.mode=allowlist` for appliance nodes (e.g. only `nvidia-smi`, `ollama …`). Such a node (like one with exec
   disabled, `shell.run` in `policy.deny_actions`, or `shell.run` not matched by `policy.allow_actions`) applies
   `fs.deny_write` to every token, operator and hub tokens included, so a file write cannot turn into code
   execution at the next login.
5. Use the `cautious` preset (or `--cautious` on `hive node token add` / `hive hub add-agent`) when agents browse
   the web; approve once or `--for 30m`. Cautious stops unapproved code execution, not overwrites: put data you
   cannot lose in read-only roots (`--root D:\photos:ro`).
6. Treat the hub's machine as the most sensitive one: any agent with exec there can act as the hub (every node,
   every approval, every auto-update pod). Give agents on the hub machine `--cautious` tokens, or run the hub on a
   machine where agents get no exec. If the hub runs on the Windows GPU desktop, its WSL node counts as the hub's
   machine: agents that run commands on the WSL node (e.g. `desk-wsl`) need `--cautious` hub tokens, or set
   `[automount] enabled=false` in WSL.
7. `hive node pause` / `panic`; review `hive audit tail`.
8. Keep tokens in `hive node secret set` (bound to their hosts; `--no-exec` for tokens only downloads need), not in
   shell profiles or service `env`.
9. Keep model servers inside the WSL node or containers; disable WSL interop and automount if the WSL node should be
   a boundary; remember Docker group = root.
10. Prefer Tailscale Funnel (a real certificate) over self-signed hubs for pods.
11. Narrow the hub's token on nodes where a compromised hub would matter (`hive node token add hub-home --kind hub
    --role files`, or `--classes read,write,net` to keep the model gateway working: its passthrough is authorized as
    `model.forward`, which needs `net`), and `relay.role` on pods. A narrowed `relay.role` turns `relay.auto_update`
    off by default (upgrade such pods by hand), and `files`/`readonly` also turn off the model gateway on that pod
    (it needs `net`): if chat apps use the pod, narrow it with `relay.deny_actions` instead.
12. `fs.deny_write` keeps identities without `exec` from writing shell profiles, autostart folders and similar; the
    built-in list covers common autoruns only, so add your own paths there (e.g. a project's `Makefile` that a cron
    job runs).
13. Gateway (`models`) tokens: one per app, listing only the services that app needs; revoke with
    `hive gateway token revoke NAME`.
14. If local admin APIs run on a node (Clash/Mihomo controller, Docker TCP socket), add them to `net.http_deny` as
    `HOST:PORT` (e.g. `"127.0.0.1:9090"`): loopback GETs are open to every token with `net`.

## Stopping everything

`hive hub stop` cuts every hub-routed agent at once; then `hive node panic` on each machine — the WSL node too
(`wsl -d <distro> -- <hive> node panic --yes`), pods through their terminal (`/workspace/.hive/hive node panic
--yes`). If a hub routes agents to a paused node, also run on the hub: `hive approvals revoke --all --hub`.

## Reporting

Hive is a personal tool. If you find a way around these rules, open an issue without exploit details first.
