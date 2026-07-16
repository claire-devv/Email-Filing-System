# Deployment notes — multi-entity filing + attachment/large-PDF guards

These changes ship via `git pull` + restart. `.env` is **gitignored**, so the server's `.env`
must be edited by hand. Below is exactly what to set/check on the Hetzner box.

## 1. Server `.env` — SET / CHECK these (production behavior)

| Var | Set to | Why | If left unset |
|-----|--------|-----|---------------|
| `MAX_AUTO_SPLIT_ENTITIES` | `4` | auto-split up to 4 clients/email | **defaults to 3** (must set to get 4) |
| `CLAUDE_MAX_PROMPT_CHARS` | `120000` | fit ~10 attachments' previews | code default is now 120000 — **but if the server `.env` still has `=60000`, UPDATE it** |
| `CLAUDE_ARTIFACT_PAYLOAD_CHARS` | `90000` | per-attachment payload block | default 90000 applies if unset (fine) |
| `CLAUDE_PDF_TOTAL_MAX_MB` | `24` | combined image budget / no context overflow | default 24 applies if unset (fine) |
| `CLAUDE_PDF_MAX_PAGES` | `100` | now ENFORCED — over-limit PDFs truncated to first 100pp | default 100 applies if unset (fine) |

Most have safe code defaults; the two that **must be checked** are
`MAX_AUTO_SPLIT_ENTITIES` (add `=4`) and `CLAUDE_MAX_PROMPT_CHARS` (raise an old explicit `60000`).

## 2. Server `.env` — must NOT contain these (local-testing only)

| Var | Local value | Server should be |
|-----|-------------|------------------|
| `PROCESS_NEW_ONLY` | `true` | **absent / false** (retries must run in prod) |
| `CLAUDE_DAILY_CALL_LIMIT` | `25` (test) | the server's real production limit |

(`ENABLE_REAL_GOOGLE=true`, `ANTHROPIC_API_KEY`, Drive/Gmail settings stay as the server already has them.)

## 3. Code / build

- **Backend:** `git pull` then restart the service (systemd). No DB migration needed — all new
  data rides in existing JSON columns. No new Python deps (`pypdf` already in requirements).
- **Frontend:** `npm run build` and publish `dist/` to nginx (the split UI + activity-feed
  per-row links are frontend changes).

## 4. Post-deploy smoke check

1. A normal single-entity email still auto-files (no regression).
2. A confident 2–4 client email auto-splits (each report in its own folder, email PDF in each
   Communications).
3. A 5+ client or low-confidence multi-entity email lands in Needs Review and shows the
   per-attachment **File Split** panel.
4. Activity feed: each split row's "Open folder in Drive" opens its own client folder.
5. (If available) an email with a 100+ page scanned attachment processes without an Anthropic
   "too many pages" error.
