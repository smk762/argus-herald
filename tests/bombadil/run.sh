#!/usr/bin/env bash
# Bombadil property tests for the review UI: light theme, dark theme, and token-locked.
#
#   tests/bombadil/run.sh            # 60 s per run
#   TIME_LIMIT=5m tests/bombadil/run.sh
#   BOMBADIL_ARGS=--no-sandbox ...   # CI runners where Chrome's sandbox can't start
#
# Needs: npm install (in this folder), Python with the project installed, and Chrome or Chromium
# on PATH (or CHROME=/path/to/chrome). Results land in tests/bombadil/out/<run>; inspect one with
#   npx bombadil browser inspect tests/bombadil/out/<run>
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
time_limit="${TIME_LIMIT:-60s}"
python="${PYTHON:-python3}"
bombadil="$here/node_modules/.bin/bombadil"
[[ -x "$bombadil" ]] || { echo "Run npm install in $here first" >&2; exit 2; }

if [[ -n "${CHROME:-}" ]]; then
  shim="$(mktemp -d)"
  ln -s "$CHROME" "$shim/chromium"
  export PATH="$shim:$PATH"
fi

server=""
cleanup() { [[ -n "$server" ]] && kill "$server" 2>/dev/null || true; }
trap cleanup EXIT

run() {
  local name="$1" spec="$2"; shift 2
  local ready
  ready="$(mktemp)"
  rm -f "$ready"
  "$python" serve.py --ready-file "$ready" "${SERVE_ARGS[@]}" >"$here/out/$name.server.log" 2>&1 &
  server=$!
  for _ in $(seq 1 100); do [[ -s "$ready" ]] && break; sleep 0.1; done
  [[ -s "$ready" ]] || { echo "serve.py did not start; see out/$name.server.log" >&2; exit 2; }
  local url
  url="$(cat "$ready")"
  echo "== $name: $url ($time_limit)"
  local status=0
  "$bombadil" browser test --headless --exit-on-violation --time-limit "$time_limit" \
    --output-path "$here/out/$name" --output-path-overwrite ${BOMBADIL_ARGS:-} "$@" "$url" "$spec" || status=$?
  kill "$server" 2>/dev/null || true
  wait "$server" 2>/dev/null || true
  server=""
  rm -f "$ready"
  return "$status"
}

mkdir -p "$here/out"
failed=0
SERVE_ARGS=()
run light review.spec.ts --cookie theme=light || failed=1
run dark review.spec.ts --cookie theme=dark || failed=1
SERVE_ARGS=(--token "$(head -c 24 /dev/urandom | base64 | tr -d '/+=')")
run locked locked.spec.ts || failed=1
exit "$failed"
