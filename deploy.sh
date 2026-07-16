#!/usr/bin/env bash
# RRES production deploy. Run as the `deploy` user from the repo root:
#   cd /srv/rres/app && ./deploy.sh
#
# This exists because past deploys broke in ways a single script structurally prevents:
#   - Mixed root/deploy ownership broke git pull + npm install -> preflight check below
#   - A restart command pasted after `exit` in an SSH session never ran, leaving stale code
#     live for hours -> every step below runs in ONE script invocation, nothing to reorder
#   - "It built, so it must be deployed" assumptions -> real content-based smoke test at the end
#
# Requires passwordless sudo for the one restart command below. One-time setup if you get a
# password prompt: as root, run `visudo` and add:
#   deploy ALL=(root) NOPASSWD: /bin/systemctl restart rres-backend, /bin/systemctl is-active rres-backend

set -euo pipefail
cd "$(dirname "$0")"

# Running this as root is exactly how the ownership whack-a-mole kept recurring: it "works" (root
# can write anything) but leaves frontend/dist and git objects root-owned, breaking the NEXT
# deploy.sh run done as `deploy`. Refuse outright instead of relying on remembering not to.
if [ "$(id -u)" -eq 0 ]; then
  echo "ERROR: do not run deploy.sh as root."
  echo "Switch to the deploy user first: su - deploy   (or SSH in directly as deploy)"
  echo "Running as root leaves files root-owned and breaks the next deploy done as 'deploy'."
  exit 1
fi

echo "== Preflight: repo ownership =="
fail_ownership() {
  echo "ERROR: $1"
  echo "Likely cause: a previous deploy ran as root and left root-owned files behind."
  echo "Fix (run once, as root): chown -R deploy:deploy $(pwd)"
  exit 1
}
# A shallow top-level touch-test isn't enough: vite's build recreates frontend/dist/assets/ as a
# brand-new nested directory each run, so a single root-run build can leave THAT subdirectory
# root-owned while the parent frontend/dist/ (created earlier, untouched by rimraf's rmdir) stays
# deploy-owned -- a shallow check passes while the actual build still fails deep inside. Scan the
# whole tree instead.
if ! touch .deploy-write-test 2>/dev/null; then
  fail_ownership "$(whoami) cannot write to $(pwd)."
fi
rm -f .deploy-write-test
if [ -d frontend/dist ]; then
  stray=$(find frontend/dist -not -user "$(whoami)" -print -quit 2>/dev/null || true)
  if [ -n "$stray" ]; then
    fail_ownership "frontend/dist contains files not owned by $(whoami) (e.g. $stray)."
  fi
fi

echo "== 1/5 git pull =="
git pull origin main

echo "== 2/5 backend dependencies =="
cd backend
./venv/bin/pip install -q -r requirements.txt
cd ..

echo "== 3/5 restart backend =="
sudo systemctl restart rres-backend
sleep 2
if ! sudo systemctl is-active --quiet rres-backend; then
  echo "ERROR: rres-backend did not come up. Check: journalctl -u rres-backend -n 50 --no-pager"
  exit 1
fi
echo "backend: active"

echo "== 4/5 build frontend =="
cd frontend
npm install --silent
npm run build
cd ..

echo "== 5/5 smoke test =="
api_body=$(curl -fsS https://api.rresai.com/health || echo "REQUEST_FAILED")
app_body=$(curl -fsS https://file.rresai.com/ || echo "REQUEST_FAILED")

ok=true

# Checks the response BODY, not just the HTTP status -- nginx's default placeholder page also
# returns 200, which is exactly how the file.rresai.com domain-switch gap went unnoticed earlier.
if [[ "$api_body" != *'"status":"ok"'* ]]; then
  echo "FAIL: api.rresai.com/health did not return the expected body. Got: $api_body"
  ok=false
else
  echo "OK: api.rresai.com is healthy"
fi

if [[ "$app_body" != *"RRES Dashboard"* ]]; then
  echo "FAIL: file.rresai.com did not return the app (nginx may be routing to the wrong site)."
  ok=false
else
  echo "OK: file.rresai.com is serving the app"
fi

if [ "$ok" = false ]; then
  echo ""
  echo "Deploy ran but the smoke test FAILED -- do not assume this deploy succeeded."
  exit 1
fi

echo ""
echo "Deploy complete and verified."
