# RRES Email Filer — Agent Handoff / Context Document

This document gives a new AI agent the full background needed to work on this project
correctly. Read it before making any change.

---

## 1. What this project is

**RRES (Rock Real Estate Services) Email Filer** — an automated system that:
1. Watches a Gmail inbox (`file@rockreservices.com`) for incoming/forwarded emails.
2. Converts each email + its attachments to PDF.
3. Uses Claude (Anthropic API) to classify which **client/entity** folder and **category**
   each document belongs to.
4. Files the PDFs into the correct **Google Drive** folders, renaming them to a standard format.
5. Anything it's unsure about goes to a **Needs Review** queue in a web dashboard for a human
   to approve/correct.

It's a real estate bookkeeping filing assistant. The client is Matthew Rodrigue (RRES).

---

## 2. ⚠️ CRITICAL: Correct working folder

The RRES repo is cloned in **TWO** places on this machine with **different git histories**.

- ✅ **USE THIS:** `D:\Malind Tech\Matt's frontend\Matts` (branch `main`, the advanced/correct state)
- ❌ **DO NOT USE:** `C:\Users\ataur\Documents\Claude Cowork\Matt's Project` (older, WRONG state — editing it once caused a full rework)

The session may *open* with the C: path as the primary working dir, but **all code changes go
to the D: path**. Always confirm a file exists under `D:\Malind Tech\Matt's frontend\Matts`
before editing.

GitHub remote: `https://github.com/ataur-rehman/Matt-s-Project.git` (remote name `origin`).
(There's an old `old-origin` pointing at `claire-devv/Matt-s-Project.git` — ignore it.)

---

## 3. Repo structure (monorepo)

```
D:\Malind Tech\Matt's frontend\Matts\
├── backend/          # FastAPI app (Python). Run with backend/ as the working dir.
│   ├── .venv/        # virtualenv (has weasyprint, anthropic, etc.)
│   ├── .env          # secrets — GITIGNORED, edited by hand on each machine/server
│   ├── app/
│   │   ├── main.py
│   │   ├── core/config.py         # settings (CWD-relative paths)
│   │   ├── api/routes/            # FastAPI routers (review.py, activity.py, admin.py, ...)
│   │   ├── services/              # the core logic (see §5)
│   │   ├── schemas/               # Pydantic response/request models
│   │   ├── db/                    # SQLAlchemy models + session
│   │   └── scripts/               # test_*.py scripts + maintenance scripts
│   └── DEPLOY_NOTES.md            # env vars to set on the server
└── frontend/         # React + Vite dashboard
    ├── .env          # VITE_API_BASE_URL (single var)
    ├── public/       # favicon/logo (logo03.png etc. — served at site root)
    ├── assets/       # source logos (must be copied to public/ to be served)
    └── src/
        ├── api/client.js          # ALL backend calls go through here
        ├── hooks/useRresData.js   # TanStack Query hooks (polling, caching)
        ├── utils/                 # datetime.js, tokenUtils.js, openBlob.js
        └── pages/dashboard/sections/  # ReviewSection.jsx, ActivitySection.jsx, etc.
```

---

## 4. How to run & deploy

### Run locally
- **Backend:** `cd backend; .venv\Scripts\python.exe -m uvicorn app.main:app --port 8088`
  (config paths are CWD-relative, so you MUST run from `backend/`)
- **Frontend:** `cd frontend; npm install; npm run dev` → port 5173. `npm run build` → `dist/`.
- All frontend API calls use a single **`VITE_API_BASE_URL`** (in `frontend/.env`, default
  `http://127.0.0.1:8088`).

### Verify changes (DO THIS instead of using a browser preview — see §8)
- Backend: `cd backend; .venv\Scripts\python.exe -c "from app.main import app; print('ok')"`
- Backend tests live in `backend/tests/` (run as modules): `cd backend; .venv\Scripts\python.exe -m tests.test_email_artifacts`
  (others: `tests.test_multi_entity_filing`, `tests.test_review_file_split`, `tests.test_pdf_guards`,
  `tests.test_reconcile_concurrency`, `tests.test_activity_row_split`, `tests.test_upload_ingest`).
  Diagnostics (`test_claude_key`) stay in `app/scripts/`.
- Frontend: `cd frontend; npm run build` (must pass lint + bundle)

### Deploy to production (Hetzner server, `root@rres-prod`, app at `/srv/rres/app`)
The app is **deployed and live** — there is no local preview of production. Deploy is:
```bash
cd /srv/rres/app && git pull                  # update files on disk (NOT live yet)
cd /srv/rres/app/frontend && npm run build     # rebuild frontend served files
systemctl restart <backend-service>            # ⚠️ REQUIRED — loads new backend code
```
**Key facts about deploy:**
- `git pull` alone does NOT make anything live. The backend is a running Python process that
  keeps the OLD code in memory until `systemctl restart`. The frontend serves the OLD built
  `dist/` until `npm run build`.
- The backend does NOT "build" (it's Python). Its deploy = pull + restart. Only run
  `pip install -r requirements.txt` if a dependency was added, or `alembic upgrade head` if a
  migration was added — neither is needed for typical service-file edits.
- `.env` is gitignored — server env vars are set by hand. See `backend/DEPLOY_NOTES.md` for the
  required vars (e.g. `MAX_AUTO_SPLIT_ENTITIES=4`, `CLAUDE_MAX_PROMPT_CHARS=120000`,
  `PROCESS_NEW_ONLY` must be ABSENT in prod).
- The backend service name is not documented here — find it with
  `systemctl list-units --type=service | grep -iE 'rres|uvicorn|gunicorn'`.
- Reprocessing: code fixes only affect NEWLY processed emails. Items already in Needs Review
  were parsed under the old code and must be resent/reprocessed to pick up a fix.

---

## 5. Architecture — the email processing pipeline

Entry point: `processing_service.process_message()`. Flow:

1. **`gmail_service.py`** — fetches the email, downloads parts, and **classifies each part** as a
   `real_attachment` vs an `inline_asset` (logos/signatures embedded in the body). This split is
   done in `_classify_part`. Only `real_attachment`s become filable `kind="attachment"` artifacts;
   `inline_asset`s are embedded into the email-body PDF.
2. **`pdf_service.py`** — `prepare_email()` builds PDFs: an `email_body` PDF (with inline images
   embedded via WeasyPrint cid data-URIs), one PDF per real attachment (`kind="attachment"`), and
   a `combined_package` PDF (whole email + attachments). Image attachments are wrapped to PDF via
   `img2pdf`. PDFs/images that have no extractable text set `requires_claude_pdf=True` so they're
   sent to Claude's **vision** (Claude reads them as images).
3. **`classifier_service.py`** — builds a big prompt (known entities, learned mappings, contact
   hints, text previews) + attaches vision PDFs, calls Claude, parses JSON. Produces a
   `ClassificationResult` with per-attachment entity/level2/level3/summary in `decision_audit`.
4. **`decision_service.py`** — `validate()` applies guardrails: confidence thresholds, valid
   Level 2/3, known vs unknown entity, and the **multi-entity auto-split gate**. Decides
   `file` / `needs_review` / `reject`.
5. **`filing_service.py`** — `file_email_artifacts()` files each artifact to its Drive folder
   (dedup/move/rename via `upload_pdf_once`), fans the combined email PDF out to every involved
   entity's **Communications** folder, and writes one `FilingLog` per entity.
6. **`review_service.py`** — handles the dashboard's Approve / Correct / Reject / **file-split**
   actions for Needs Review items, then reuses `filing_service` to actually file.

### Key domain rules (the "filing schema")
- **Dual filing:** the whole-email PDF ALWAYS goes to the entity's **Communications** folder; each
  real attachment goes to ITS OWN category (Bank Statements, Insurance, Property Taxes, Leases,
  Client Reporting, etc.) — never Communications.
- **Multi-entity:** one email can carry documents for several clients. The system auto-splits
  (files each attachment to its own client) when confident about every attachment, up to
  `MAX_AUTO_SPLIT_ENTITIES` (4 in prod). Otherwise → Needs Review with a per-attachment "File
  Split" UI (`ReviewSection.jsx` + `review_service.file_split`).
- **File naming (Drive):** `YYYY.MM.DD - {summary including entity/property reference}.pdf`.
  - Date prefix from the document's own date when readable, else the email received date.
  - Summary must NOT contain a date/year (the prefix carries it). `_trim_redundant_date` strips
    stray dates; there's a mirror `trimRedundantDate` in `ReviewSection.jsx`.
  - Communications email archive is named `YYYY.MM.DD - {Sender} - {Subject}.pdf`.
- **Building-number rule:** an address matches a known entity ONLY if the building NUMBER is
  identical (1416 Frankford ≠ 1828 Frankford). The prompt enforces this strictly.
- **Owner vs counterparty:** the Level-1 principal is the property OWNER, never a lender/servicer/
  title/escrow counterparty. Prompt + `_role_for_contact` enforce this.

---

## 6. Work done in this session (recent commits on `main`)

Most recent first:
- **`0fdaf47`** — `pdf_service.py`: image attachments now set `requires_claude_pdf=True` so
  screenshots/photos of documents are sent to Claude's vision and actually read (previously they
  had no text and were never sent → "no readable text preview" + dumped to review).
- **`e601ceb`** — `gmail_service.py`: a cid-referenced image is only treated as inline decoration
  when it's non-image, signature-sized, or generically auto-named (`imageNNN`). A
  descriptively-named, document-sized cid image (e.g. an iPhone `Screenshot ….png` attached in
  Mail) now becomes a `real_attachment` (flagged `ambiguous_image_part` → routes to review) so it
  files to its category instead of being buried in Communications. + `ReviewSection.jsx`: the
  Drive-filename preview now builds its date prefix from the reviewer's Document-date field
  (single-entity form + split rows), so the preview tracks date edits.
- **`0ede66c`** — `ReviewSection.jsx` UX: `canFile` requires Level 3 when the subfolder needs it;
  `reviewed_by` (logged-in user email) sent on all review actions; component owns its own 30s
  polling via `useReviewItems`; `AUTO_FILE_CONFIDENCE` constant replaces hardcoded `80`.
- Earlier commits: favicon/logo wiring (`logo03.png`/`logo04.png` in `frontend/public/`).

### The driving test case ("Di Vita")
Email "Fwd: Prop tax receipt 2026 Di Vita" — a property-tax receipt for Barr Family Trust /
6327 Di Vita Drive, sent as an **iPhone screenshot attached in Mail** (`Screenshot 2026-06-26 at
1.28.11 AM.png`). It exposed two bugs both now fixed: (a) the screenshot was misclassified as an
inline image and lost its Property Taxes filing → fixed in `e601ceb`; (b) even as an attachment
it had no text and Claude couldn't read it → fixed in `0fdaf47`.

---

## 7. Known issues / pending work

- **"Year in summary" (Change B — NOT done, deliberately skipped):** Claude sometimes still emits
  a year in the file summary, e.g. `Property Tax Receipt 2026 for Di Vita`. The proposed fix was a
  3rd regex pass in `_trim_redundant_date` (`re.sub(r"(?<!\d)(?:19|20)\d{2}\b(?=\s+for\b)", "")`)
  + mirror in `trimRedundantDate` + a BAD/GOOD example in the classifier prompt. The user chose to
  skip it for now — revisit if asked.
- **Multi-entity split UI (Phase 2):** code-complete and unit-tested, but still wants a real
  end-to-end live test of an actual split through the Needs Review "File each separately" button.
- **Drive upload-folder scanning (client-requested, NOT built):** the client wants a "Client
  Uploads" folder inside each client folder + a single "RRES Uploads" folder for the team; the
  system should scan, rename, and MOVE files into the correct folders using the same naming
  scheme. This is a sizable new feature, not yet started.
- **Brand colors:** the client sent a logo (done) and brand color codes — confirm whether the
  brand colors were ever applied to the dashboard CSS.

---

## 8. Gotchas / working preferences (IMPORTANT)

- **No browser preview for this project.** It's deployed remotely (`app.rresai.com` /
  `api.rresai.com`); the chat preview pane is sandboxed to localhost and can't load it. Don't use
  `preview_*` tools or tell the user to check a preview pane. Verify with `npm run build` /
  `python -c "from app.main import app"` and let the user test on the real deployed URL.
- **Timezone:** dashboard shows **US Eastern**. Backend emits every datetime as UTC ISO-8601 with
  a trailing `Z` (`UtcDateTime` type in `schemas/common.py`); frontend `utils/datetime.js`
  converts to `America/New_York`. Don't reintroduce bare `new Date(x).toLocaleString()`.
- **WeasyPrint native libs:** rendering the email-body PDF (with inline images) needs GTK/Pango/
  cairo DLLs at `C:\Users\ataur\gtkdll\bin`, wired via `WEASYPRINT_DLL_DIRECTORIES` in
  `backend/.env`. Without it, `import weasyprint` fails and inline images vanish (falls back to
  text). Restart the server after editing `.env`.
- **Frontend↔backend filename mirror:** `_trim_redundant_date` (Python) and `trimRedundantDate`
  (JS), and `_filename`/`driveFilenamePreview`, are mirrors. Edit BOTH together or the live
  preview drifts from what's actually filed.
- **Logos must be in `frontend/public/`** to be served (not just `assets/`); filenames with spaces
  (`logo03 - Copy.png`) don't serve reliably through nginx — use space-free names.
- **PowerShell git commits:** multi-line commit messages via here-strings (`@'...'@`) break when
  chained with `;`. Run `git commit` as its OWN standalone PowerShell command, with the closing
  `'@` at column 0.
- **Dashboard auth:** JWT bearer login. Dev creds live in `backend/.env`
  (`DASHBOARD_AUTH_EMAIL`/`DASHBOARD_AUTH_PASSWORD`) — these are placeholders the client should
  change. Deployed frontend origin must be in backend `CORS_ORIGINS`.

---

## 9. Test environment notes

- Backend Google OAuth token is for the test account **clairelacombe70@gmail.com** ("Claire").
  Test emails forwarded by the client often land elsewhere and must be forwarded to Claire's inbox
  to be processed.
- `DRIVE_ROOT_ID` = shared drive `0ANHZbkzoqG6mUk9PVA` ("RRES - File Test_Claire"), ~28 master
  client folders named `[Initial]. [Last] - [Company]`.
- The client's own filing-rules doc lives on Drive at
  `J. Zimmerman - Fort Impact Fund LLC (FIF)/.claude/agents/books-classifier.md` (copy kept at
  `runtime/books-classifier.md`) — richer than `data/folder_structure.json`; it has the strict
  per-category filename templates, standardized bank/lender names, dual-filing rule, etc.

---

## 10. Quick orientation checklist for a new agent

1. Confirm you're editing under `D:\Malind Tech\Matt's frontend\Matts` (NOT the C: clone).
2. Read the relevant `app/services/*.py` end-to-end before changing pipeline behavior — the logic
   is interconnected (classifier ↔ decision ↔ filing share `decision_audit`).
3. After any change: `npm run build` (frontend) and/or `python -c "from app.main import app"` +
   the relevant `tests.test_*` (backend, run as `python -m tests.test_<name>`).
4. Commit only the files you changed; don't sweep up the many pre-existing unstaged changes in the
   working tree.
5. Deploy = `git pull` + `npm run build` + **`systemctl restart <backend-service>`**. The restart
   is what makes backend changes live.
