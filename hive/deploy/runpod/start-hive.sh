#!/bin/sh
# Hive pod start command (design §20.4, WP4). POSIX sh + an inline stdlib `python3 -` bootstrap.
#
# Template env: HIVE_JOIN='hvj1.…' (or HIVE_RELAY_URL [+ HIVE_RELAY_PIN] + HIVE_RELAY_TOKEN), optional
# HIVE_PYZ_SHA256, HIVE_SECRET_<NAME> / HIVE_SECRET_HOSTS_<NAME>, HIVE_PROXY, HIVE_RELAY_PROXY, HIVE_HF_ENDPOINT.
# Start command:  sh /workspace/start-hive.sh   (or paste this file into the template's start script)
#
# 1. HIVE_HOME defaults to /workspace/.hive when /workspace exists (node_id, instance_key, services.json and
#    secrets survive pod restarts there).
# 2. Without $HIVE_HOME/hive.pyz the bootstrap fetches <hub>/v1/dist/hive.pyz (no token is sent; through
#    HIVE_RELAY_PROXY, else HIVE_PROXY; CONNECT for https), verifying TLS with the system CAs or, with a pin in the
#    join code / HIVE_RELAY_PIN, the certificate's DER sha256 before any byte is sent; the body's sha256 must equal
#    the hub's X-Hive-Sha256 header and, when set, HIVE_PYZ_SHA256. It is written as hive.pyz.new, then renamed.
#    A plain http:// hub URL is accepted only for a loopback/LAN/tailnet host, unless HIVE_PYZ_SHA256 is set.
# 2a. cd "$HIVE_HOME" and write the $HIVE_HOME/hive wrapper when missing.
# 3. Loop like the watchdog (HIVE_WATCHDOG=1): `hive.pyz _update` (always; it updates only when relay.auto_update),
#    `hive.pyz node run` (env-only mode reads HIVE_JOIN, HIVE_SECRET_*, HIVE_PROXY, … itself), exit 0 -> stop,
#    anything else -> sleep 5 and run again.
# HIVE_PYTHON (default python3) and HIVE_POD_WORKSPACE (default /workspace) exist for tests.
set -u

WS="${HIVE_POD_WORKSPACE:-/workspace}"
if [ -z "${HIVE_HOME:-}" ]; then
  if [ -d "$WS" ]; then HIVE_HOME="$WS/.hive"; else HIVE_HOME="${HOME:-/root}/.hive"; fi
fi
export HIVE_HOME
PY="${HIVE_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "start-hive: $PY not found" >&2; exit 1; }
umask 077
mkdir -p "$HIVE_HOME" || exit 1
chmod 700 "$HIVE_HOME" 2>/dev/null || true

# -I: isolated mode, so a json.py/ssl.py in the start cwd (often /workspace, agent-writable) is never imported (D56)
if [ ! -f "$HIVE_HOME/hive.pyz" ]; then
  "$PY" -I - <<'HIVE_BOOTSTRAP_PY' || { echo "start-hive: could not fetch hive.pyz (nothing written)" >&2; exit 1; }
import base64, hashlib, http.client, json, os, socket, ssl, sys, urllib.parse


def fail(msg):
    sys.stderr.write("start-hive: " + msg + "\n")
    sys.exit(1)


home = os.environ["HIVE_HOME"]
url = pin = None
code = (os.environ.get("HIVE_JOIN") or "").strip()
if code:
    if not code.startswith("hvj1."):
        fail("HIVE_JOIN is not a hvj1 join code")
    raw = code[5:]
    try:
        d = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
        url, pin = d["u"], d.get("p")
    except Exception:
        fail("HIVE_JOIN could not be decoded")
url = url or os.environ.get("HIVE_RELAY_URL")
pin = pin or os.environ.get("HIVE_RELAY_PIN") or None
if not url:
    fail("set HIVE_JOIN (or HIVE_RELAY_URL) to fetch hive.pyz from the hub")
u = urllib.parse.urlsplit(url)
if u.scheme not in ("http", "https") or not u.hostname:
    fail("the hub URL must be http(s)://host[:port]")
host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)


def plain_ok(h):
    # §5.9 rule C1 (as relay.check_hub_url): plaintext only to a loopback/private/tailnet address
    import ipaddress
    cgnat = ipaddress.ip_network("100.64.0.0/10")

    def ip_ok(a):
        try:
            ip = ipaddress.ip_address(a.split("%", 1)[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_loopback or (ip.version == 4 and ip in cgnat):
            return True
        if ip.version == 6 and ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
            return True
        return ip.is_private and not ip.is_link_local and not ip.is_unspecified and not ip.is_reserved
    h = h.strip("[]")
    if h.lower() in ("localhost",) or ip_ok(h):
        return True
    try:
        ipaddress.ip_address(h.split("%", 1)[0])
        return False        # an IP literal that is not private
    except ValueError:
        pass
    try:
        addrs = [ai[4][0] for ai in socket.getaddrinfo(h, port, 0, socket.SOCK_STREAM)]
    except (OSError, UnicodeError):
        addrs = []
    if addrs:
        return all(ip_ok(a) for a in addrs)
    low = h.lower().rstrip(".")
    return "." not in low or low.endswith((".ts.net", ".local", ".lan", ".home.arpa", ".internal"))


# Phase 2 review: code fetched over plain http from a public address can be swapped on the path; without a
# pinned HIVE_PYZ_SHA256 only a loopback/LAN/tailnet hub may serve it
if u.scheme == "http" and not (os.environ.get("HIVE_PYZ_SHA256") or "").strip() and not plain_ok(host):
    fail("refusing to fetch hive.pyz over plain http from %s (not a LAN/tailnet address); use an https hub URL "
         "or set HIVE_PYZ_SHA256" % host)
path = (u.path or "").rstrip("/") + "/v1/dist/hive.pyz"
hostport = ("[%s]" % host if ":" in host else host) + ":" + str(port)
proxy = None
for name in ("HIVE_RELAY_PROXY", "HIVE_PROXY"):
    val = (os.environ.get(name) or "").strip()
    if val.lower() == "off":
        break
    if val and val.lower() != "auto":
        proxy = val
        break
want_pin = None
if pin:
    want_pin = pin.lower().replace("sha256:", "").replace(":", "").strip()

sock = None
try:
    if proxy:
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if p.scheme != "http" or not p.hostname:
            fail("only http:// proxies are supported (got %s)" % p.scheme)
        sock = socket.create_connection((p.hostname, p.port or 80), timeout=30)
        auth = ""
        if p.username:
            cred = urllib.parse.unquote(p.username) + ":" + urllib.parse.unquote(p.password or "")
            auth = "Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode() + "\r\n"
        if u.scheme == "https":
            sock.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s\r\n" % (hostport, hostport, auth)).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    fail("the proxy closed the connection")
                buf += chunk
                if len(buf) > 65536:
                    fail("bad proxy answer")
            status = buf.split(b"\r\n", 1)[0].split()
            if len(status) < 2 or status[1] != b"200":
                fail("proxy CONNECT refused: " + buf.split(b"\r\n", 1)[0].decode("latin-1"))
            target = path
        else:
            target = "http://%s%s" % (hostport, path)
    else:
        sock = socket.create_connection((host, port), timeout=30)
        target = path
        auth = ""
    if u.scheme == "https":
        if want_pin:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE          # pin path: the DER sha256 is compared before any byte is sent
            sock = ctx.wrap_socket(sock, server_hostname=host)
            der = sock.getpeercert(binary_form=True) or b""
            if hashlib.sha256(der).hexdigest() != want_pin:
                fail("TLS pin mismatch for %s" % hostport)
        else:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    hdrs = "GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: hive-start\r\nAccept-Encoding: identity\r\nConnection: close\r\n" \
        % (target, hostport)
    if proxy and u.scheme == "http":
        hdrs += auth
    sock.sendall((hdrs + "\r\n").encode())
    resp = http.client.HTTPResponse(sock)
    resp.begin()
    body = resp.read()
    if resp.status != 200:
        fail("GET %s answered %d" % (path, resp.status))
    hub_sha = (resp.getheader("X-Hive-Sha256") or "").strip().lower()
except (OSError, ssl.SSLError, http.client.HTTPException) as e:
    fail("cannot reach %s: %s" % (hostport, e))
finally:
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
got = hashlib.sha256(body).hexdigest()
if not hub_sha or got != hub_sha:
    fail("hive.pyz does not match the hub's X-Hive-Sha256; not written")
pinned = (os.environ.get("HIVE_PYZ_SHA256") or "").strip().lower()
if pinned and got != pinned:
    fail("hive.pyz does not match HIVE_PYZ_SHA256; not written")
tmp = os.path.join(home, "hive.pyz.new")
with open(tmp, "wb") as f:
    f.write(body)
os.chmod(tmp, 0o600)
os.replace(tmp, os.path.join(home, "hive.pyz"))
sys.stderr.write("start-hive: fetched hive.pyz (sha256 %s)\n" % got)
HIVE_BOOTSTRAP_PY
fi

cd "$HIVE_HOME" || exit 1
if [ ! -f "$HIVE_HOME/hive" ]; then
  q_home=$(printf "%s" "$HIVE_HOME" | sed "s/'/'\\\\''/g")
  q_py=$(command -v "$PY" | sed "s/'/'\\\\''/g")
  printf "#!/bin/sh\nexport HIVE_HOME='%s'\nexec '%s' '%s/hive.pyz' \"\$@\"\n" "$q_home" "$q_py" "$q_home" \
    > "$HIVE_HOME/hive.tmp" && chmod 700 "$HIVE_HOME/hive.tmp" && mv -f "$HIVE_HOME/hive.tmp" "$HIVE_HOME/hive"
fi

export HIVE_WATCHDOG=1
mkdir -p "$HIVE_HOME/node/logs"
LOG="$HIVE_HOME/node/logs/stdout.log"
while true; do
  "$PY" "$HIVE_HOME/hive.pyz" _update >> "$LOG" 2>&1 || true
  "$PY" "$HIVE_HOME/hive.pyz" node run >> "$LOG" 2>&1
  [ "$?" -eq 0 ] && break
  sleep 5
done
