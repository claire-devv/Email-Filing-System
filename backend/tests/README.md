# Backend tests

Offline regression tests for the RRES backend. They are **standalone scripts** (not pytest) — each
builds an in-memory SQLite DB, fakes the Drive/Claude/Gmail layers, runs assertions, and prints
`"<name>: all assertions passed"`. None call the real Claude, Gmail, or Drive APIs.

## Running

Run from the `backend/` directory (the package root) as a module:

```powershell
cd backend
.venv\Scripts\python.exe -m tests.test_upload_ingest
.venv\Scripts\python.exe -m tests.test_multi_entity_filing
.venv\Scripts\python.exe -m tests.test_review_file_split
# ...etc
```

Run them all (PowerShell):

```powershell
cd backend
Get-ChildItem tests\test_*.py | ForEach-Object {
  $m = "tests." + $_.BaseName
  & .venv\Scripts\python.exe -m $m > $null 2>&1
  if ($?) { "PASS  $m" } else { "FAIL  $m" }
}
```

## What's here

| Test | Covers |
|---|---|
| `test_decision_validator` | confidence/entity/category gating, reject rules |
| `test_multi_entity_filing` | multi-entity auto-split gate + per-attachment resolver |
| `test_review_file_split` | the Needs Review "File each separately" path |
| `test_upload_ingest` | Drive upload-folder scanning (move/dedup/extension/trash/review, single-doc prep, upload prompt framing, wrong-folder safety net) |
| `test_email_artifacts` | email → PDF prep, inline-image filtering, prompt budget/format |
| `test_pdf_guards` | attachment-heavy / large-PDF page-truncation guards |
| `test_api_limit_handling` | Claude daily-limit parking + retry |
| `test_reconcile_concurrency` | on-demand entity reconcile under a concurrent burst |
| `test_activity_row_split` | per-row Drive-folder scoping in the activity feed |
| `test_review_learning_notifications` | notification + feedback-learning flow |
| `test_google_auth_tokenfile` | token.json concurrent-write safety + corruption-tolerant reads |

## Not here (stay in `app/scripts/`)

- `test_claude_key.py` — a **live** diagnostic that checks your `ANTHROPIC_API_KEY` actually works
  (it calls the real API), so it isn't an offline regression test.
- `phase1_test_helper.py` — a runner/utility, not a test.
