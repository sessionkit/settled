#!/bin/bash
# Drive app/webxdc.js in headless Chrome against the real web.main.
#   tests/browser/run.sh https   # self-signed TLS on a spare port: the vm_session path
#   tests/browser/run.sh http    # plain localhost: the sessionStorage token path
#   tests/browser/run.sh enrol   # plain localhost, the store holding only another host's
#                                # passkey: the enrol sheet and its one-time code
# Spare ports, temp passkey/session stores, a temp Chrome profile: nothing live is
# touched. Needs Google Chrome, node >= 22 (global WebSocket/fetch), openssl, and a
# Python with websockets ($PY, default python3). Not collected by pytest.
set -u; MODE=${1:?https, http or enrol}; HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
PY=${PY:-python3}; CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
T=$(mktemp -d); spare() { $PY -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
P=$(spare); C=$(spare); Q=$(spare); OTHER=-; OSRV=
if [ "$MODE" = https ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$T/key.pem" -out "$T/cert.pem" -days 1 -subj /CN=localhost \
    -addext subjectAltName=DNS:localhost 2>/dev/null
  TLS=(tls "$T/cert.pem" "$T/key.pem"); B=https://localhost:$P
  # A plain page on a SECOND port, same site (localhost), so SameSite=Strict sends
  # vm_session on its fetches: the other-port probe runs from here
  mkdir -p "$T/other"; echo '<!doctype html><title>other port</title>' > "$T/other/index.html"
  $PY -c 'import functools,http.server,ssl,sys
c=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); c.load_cert_chain(sys.argv[2],sys.argv[3])
s=http.server.ThreadingHTTPServer(("127.0.0.1",int(sys.argv[1])),functools.partial(http.server.SimpleHTTPRequestHandler,directory=sys.argv[4]))
s.socket=c.wrap_socket(s.socket,server_side=True); s.serve_forever()' "$Q" "$T/cert.pem" "$T/key.pem" "$T/other" > "$T/other.log" 2>&1 &
  OSRV=$!; OTHER=https://localhost:$Q
else
  TLS=(plain); B=http://localhost:$P
fi
export SETTINGS_ENROL=$T/enrol.json
if [ "$MODE" = enrol ]; then   # another host's passkey only, and a code from the real CLI
  printf '{"credentials": [{"id": "studio-key", "rp_id": "studio.enrol-rig.ts.net", "spki": "", "alg": -7, "added": "", "name": "Mac"}]}\n' \
    > "$T/passkeys.json"
fi
# every mode needs the code: an empty store enrols its first passkey too (no TOFU)
CODE=$(SETTINGS_PASSKEYS=$T/passkeys.json PYTHONPATH="$ROOT" $PY -m settled.web enrol | awk '/^enrol code:/{print $3}')
[ -n "$CODE" ] || { echo "FAIL $MODE: settled.web enrol printed no code"; rm -rf "${T:?}"; exit 1; }
SETTINGS_PASSKEYS=$T/passkeys.json SETTINGS_SESSIONS=$T/sessions.json $PY "$HERE/serve.py" "$ROOT" "$P" "${TLS[@]}" > "$T/server.log" 2>&1 &
SRV=$!
"$CHROME" --headless=new --remote-debugging-port=$C --user-data-dir="$T/chrome" --ignore-certificate-errors \
  --no-first-run --no-default-browser-check about:blank > "$T/chrome.log" 2>&1 &
CHR=$!
for _ in $(seq 50); do curl -s "http://127.0.0.1:$C/json/version" >/dev/null && curl -sk "$B/" >/dev/null \
  && { [ "$OTHER" = - ] || curl -sk "$OTHER/" >/dev/null; } && break; sleep 0.2; done
if [ "$MODE" = enrol ]; then node "$HERE/enrol.mjs" "$C" "$B" "$CODE"; RC=$?
else node "$HERE/drive.mjs" "$C" "$B" "$MODE" "$OTHER" "$CODE"; RC=$?; fi
echo "--- server auth log"; grep '"auth"' "$T/server.log"
if [ "$MODE" = https ]; then   # the server's own word: the probe arrived WITH the cookie and was refused
  if grep -q '"auth": "sign-out".*"result": "refused: cross-origin (Sec-Fetch-Site same-site, cookie)"' "$T/server.log"; then
    echo "PASS https: the server logged the cross-port sign-out as refused, cookie and Sec-Fetch-Site same-site present"
  else
    echo "FAIL https: no cross-origin refusal with the cookie in the server log"; RC=1
  fi
fi
kill $CHR $SRV $OSRV 2>/dev/null; wait 2>/dev/null; rm -rf "${T:?}"; echo "exit $RC"; exit $RC
