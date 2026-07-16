# Drive upload-folder scanning ("Client Uploads" + "RRES Uploads")

> **STATUS: IMPLEMENTED (backend) — pending live Drive smoke test.**
> All five steps + the review-fix items are coded and unit-tested
> (`tests/test_upload_ingest.py`, all assertions pass; `test_multi_entity_filing` and
> `test_review_file_split` still pass — no email regressions). The feature ships **off by default**
> (`UPLOADS_SCAN_ENABLED=false`); set it to `true` on the server once the "Client Uploads" /
> "RRES Uploads" folders exist and are shared. See the per-step ✅ markers below.
>
> **FOLLOW-UP DONE: uploads no longer present as emails.** Uploaded items previously rendered like
> an email (3 docs: "Email preview" + source-note cover + "Combined PDF") and Claude's reasoning
> said "the email has no sender/subject". Fixed:
> - `PdfService.prepare_single_document` builds ONLY the one document artifact (no email_body /
>   combined_package / source-note cover). `UploadIngestService` now calls it instead of
>   `prepare_email`. The email path is untouched (still uses `prepare_email`).
> - `ClassifierService._prompt` prepends a document-framing override for uploads (source read from
>   the synthetic email's `raw_metadata["source"]=="drive_upload"`): says "document" not "email",
>   tells the model not to flag missing email metadata, sets `email_sender=null`. Emails unchanged.
> - `ReviewSection.jsx` shows uploads as a single "Document" chip (filters to `kind==attachment`),
>   guarding pre-fix items too. Activity feed already filtered by `drive_file_id`, so no change.
> - Tests extended (`test_upload_ingest.py` Cases 1 & 8) to assert single-artifact prep + the
>   document/email prompt framing. All email tests (`test_email_artifacts`, etc.) still pass.

## Context

The client wants files dropped directly into Google Drive (not just emailed) to be auto-filed:
- A **"Client Uploads"** sub-folder inside *each* client folder — clients (pre-shared, no login)
  drop files there; the system files them **under that client**, working out only the category.
- A single **"RRES Uploads"** folder at the Drive root — the internal team drops files for any
  client; the system works out **both the client and the category** from content.

Mostly bank statements, settlement docs, credit-card statements, Airbnb earnings reports, etc.
Client's explicit rules: **move the whole (original) file** into the correct destination folder
(upload folders should only ever hold *unfiled* items), use the **same filing naming** as emails,
**never touch the same file twice**, and send anything uncertain to **Needs Review**.

The existing email pipeline (prepare → classify → validate → file) is **source-agnostic** once fed
a synthetic email-like object, so this feature reuses it almost entirely rather than duplicating
classification/decision/filing logic.

## Decisions (locked with user)

- **Client Uploads** → entity is FIXED to the folder's owning client; AI determines category + L3 only.
- **RRES Uploads** → AI determines entity + category + L3 (full classification, like email).
- **On confident file** → MOVE the original Drive file (byte-identical, no cover page) into the
  destination folder, renamed `YYYY.MM.DD - <Summary>.<original-ext>` — **preserve the real
  extension** (a CSV/XLSX/PNG keeps its type; only the name follows the standard format). NOT the
  email-style generated-PDF upload.
- **Source note** → tracked in the system/activity feed only; the Drive file is left byte-identical
  (no cover page, no companion file).
- **On uncertain** → leave the file in place; create a Needs Review item. Move it only on approval.
- **Dedup** → never reprocess the same Drive file twice (tracked by Drive file id).
- **Scan cadence** → every 15 minutes (background loop).

## Key reuse points (existing code)

- `ProcessingService.process_message` (`processing_service.py:60`) — the template to mirror.
- `ClassifierService.classify` (`classifier_service.py:27`) — classifies from filename + content;
  works with null sender/subject.
- `DecisionValidator.validate` (`decision_service.py`) — confidence/entity/category gating, reused
  verbatim (with entity forced for Client Uploads).
- `FilingService` — `resolve_target_folder` to get the destination folder id; `drive.move_file`
  (`drive_service.py:313`) to relocate the original; `_summary_for_artifact`/`_trim_redundant_date`
  for the name.
- `EntityService.list_active` — to map discovered Client Uploads folders to their owning entity.
- Background-loop pattern: `_safety_net_loop`/`_retry_loop` in `main.py:149-193` + `lifespan:200`.
- Dedup: unique `ProcessedEmail.gmail_message_id` + `ProcessedFile(file_hash, folder)`.
- `pdf_service.prepare_email` — still used to build the text preview + (optional) vision PDF for
  classification, even though we move the original for the final placement.

## No DB migration

`init_db` uses `create_all` (`db/session.py:50`) which does NOT add columns to the existing live
SQLite table. So DO NOT add `ProcessedEmail.source`/`source_file_id` columns. Instead:
- Use a synthetic `gmail_message_id = "drive-upload:<driveFileId>"` (unique key = dedup + provenance).
  The `drive-upload:` prefix is ALSO the isolation marker used everywhere below to keep uploads out
  of Gmail-only code paths.
- Store upload provenance (source folder id, source kind `client_uploads`/`rres_uploads`, original
  Drive file id, original filename, original extension) inside `ProcessedEmail.metadata_json`.

## Isolating uploads from Gmail-only machinery (review fixes 2 & 5)

`ProcessedEmail` is reused, so upload rows MUST be fenced off from code that assumes a Gmail id:
- **Retry loop** (`main.py:114`): the failed/`waiting_api_limit` query calls
  `process_message(gmail_message_id)` → `gmail.fetch_message(...)`, which fails forever on a
  `drive-upload:` id. Fix: add `ProcessedEmail.gmail_message_id.not_like("drive-upload:%")` to that
  query, and add a SEPARATE upload-retry branch (re-run `UploadIngestService` for failed upload
  rows under the same attempt cap).
- **Safety-net loop** (`process_unread`) is Gmail-search based and never sees upload rows — fine.
- **Gmail label calls**: `_mark_gmail_filed/skipped/failed` and `gmail.mark_*` must be skipped when
  the id starts with `drive-upload:`. Guard in `review_service` (`_mark_gmail_filed`/`_mark_gmail_skipped`)
  and in `UploadIngestService` (never call `mark_*`).
- **`UploadIngestService` must NOT call `file_email_artifacts`** (review fix 5): that method fans the
  combined-package PDF to Communications and uploads generated attachment PDFs — we want a single
  MOVE of the original. Filing for uploads is its own small path (resolve target folder → move_file
  → ProcessedFile + FilingLog). The combined_package/email_body artifacts are marked internal and
  never uploaded.

## Implementation

### 1. DriveService — add file listing + move-original helpers (`drive_service.py`) ✅ DONE
- `list_files_in_folder(folder_id) -> list[dict]` — query
  `mimeType != 'application/vnd.google-apps.folder' and '<id>' in parents and trashed=false`,
  fields `id,name,mimeType,size,md5Checksum,createdTime`, with pagination (mirror `_child_folder_names`).
- `find_upload_folders() -> list[dict]` (review fix 4 — bounded scan cost): a SINGLE drive-wide
  query `name='Client Uploads' and mimeType='...folder' and trashed=false` (+ a query for the one
  RRES Uploads folder), returning only the folders that actually exist, with their parent ids. This
  replaces "resolve + list for every entity every tick" (which is dozens of calls scaling with
  entity count). Map each returned Client Uploads folder back to its owning entity via its parent id
  against the entity registry's `drive_folder_id`. Only then list files in folders that exist.
- Reuse existing `move_file(file_id, from_folder, to_folder, new_name)` and
  `download_file_stream` (to fetch bytes for hashing + classification text extraction).

### 2. New `UploadIngestService` (`app/services/upload_ingest_service.py`) ✅ DONE
Mirrors `ProcessingService` but for one Drive file. `process_drive_upload(db, file_meta,
source_folder_id, source_kind, fixed_entity | None)`:
> Implemented as `app/services/upload_ingest_service.py` with helpers `upload_message_id()` /
> `is_upload_message_id()` (the `drive-upload:` id prefix is the isolation marker). Reuses
> `ProcessingService._persist_artifacts` / `_create_review` via composition and the existing
> `_PROCESS_SEMAPHORE`. Filing is a private `_file_upload()` that MOVES the original (NOT
> `file_email_artifacts`). The destination bar (fix 3) lives in `decision_service.UPLOAD_SOURCE_FOLDERS`
> + the classifier prompt.
1. **Dedup**: `gmail_message_id = f"drive-upload:{file_id}"`; if a ProcessedEmail with that id is
   already in a final/processing state → skip (the "never twice" guarantee).
2. Create `ProcessedEmail(status="processing")` with provenance in `metadata_json`.
3. Download file bytes → `work_dir/original/<safe filename>`; compute sha256.
4. Build a synthetic `EmailMessageData` (sender/subject/body null, `received_at` = Drive
   `createdTime`, one `EmailAttachment` = the file). Run `pdf.prepare_email` to get the text
   preview / vision PDF, then `_persist_artifacts`.
5. `classifier.classify(...)`. For **Client Uploads**, override the resolved entity to the fixed
   owning entity before validation (category/L3 still from Claude). For **RRES Uploads**, use
   Claude's entity as-is.
   - **Bar "Client Uploads"/"RRES Uploads" as filing DESTINATIONS** (review fix 3): these L2 names
     are valid *source* folders but must never be a *target* category (would leave docs "unfiled").
     Like `Communications`/`Needs Review` are already special-cased, exclude them from the category
     the classifier may pick — add to the prompt's "never file INTO" list and reject in the
     validator (treat a doc classified to Client Uploads/RRES Uploads as needs_review, or fall back
     to `Misc.`). Confirm `_attachment_target_spec`/`decision_service` enforce it.
6. `validator.validate(...)`:
   - **should_review / unknown / low-confidence** → create Needs Review item, **leave the Drive
     file in place** (do not move). Record `source_file_id` so the reviewer's approval later moves it.
   - **should_file** → resolve destination folder via `resolve_target_folder(entity, level2, level3)`,
     compute the standard name `YYYY.MM.DD - <Summary>` then append the **original file's
     extension** (not forced `.pdf`), and `drive.move_file(file_id, source_folder_id,
     dest_folder_id, new_name)`. Mark the artifact filed; write a `ProcessedFile` (hash+folder) +
     `FilingLog`. (Reuse `_summary_for_artifact` + `_trim_redundant_date` for the summary; do NOT
     reuse `_filename` verbatim since it hardcodes `.pdf`.)
7. Throttle with the existing `_PROCESS_SEMAPHORE` (reuse, don't add a new one) to bound Claude+Drive burst.

### 3. Background scan loop (`main.py`) ✅ DONE
> Added `_scan_uploads_once()`, `_retry_failed_uploads_once()` (the dedicated upload-retry branch),
> and `_uploads_scan_loop()` (registered in `lifespan` only when `uploads_scan_enabled`). The Gmail
> retry query now excludes `gmail_message_id LIKE 'drive-upload:%'` (fix 2).
- `_scan_uploads_once()`: gated on `enable_real_google + drive_root_id + uploads_scan_enabled`.
  - **Discover folders via `find_upload_folders()` only** (Step 1) — NO per-entity loop. That call
    does at most two drive-wide queries: one for all `Client Uploads` folders, one for the single
    `RRES Uploads` folder. Cost is fixed (≈2 list calls) regardless of entity count.
  - Build the entity lookup once: `{entity.drive_folder_id: entity}` from `entities.list_active`.
    For each discovered **Client Uploads** folder, map it to its owner via its `parent` id against
    that lookup; if the parent isn't a known entity folder, skip it (stray folder). Process its
    files with `source_kind="client_uploads"`, `fixed_entity=<owner>.entity_name`.
  - Process the **RRES Uploads** folder's files with `source_kind="rres_uploads"`, no fixed entity.
  - List a folder's files with `list_files_in_folder(folder_id)` (Step 1); per file: skip if already
    processed (dedup by `drive-upload:<id>`), else `UploadIngestService.process_drive_upload(...)`.
  - One `SessionLocal()`, try/except per file (a bad file never breaks the loop), `finally db.close()`.
- `_uploads_scan_loop()`: `await asyncio.sleep(interval)` then loop on `asyncio.to_thread`,
  registered in `lifespan` alongside the others.
- Note: empty Client Uploads folders cost nothing beyond the single discovery query — there is no
  per-entity resolve/list. `resolve_target_folder` is only called later, for the DESTINATION of a
  file that is actually being filed.

### 4. Review approval moves the staged upload (`review_service.py`) ✅ DONE
When a reviewer approves/corrects/splits a Needs-Review item that came from an upload (detected via
`metadata_json` provenance), after deciding the destination, **move the original Drive file** from
its upload folder to the destination (same `move_file` path), instead of the email flow's
generated-PDF upload. Keep email items behaving exactly as today.
> Implemented via a `_file_review()` dispatcher: email items still call `file_email_artifacts`;
> upload items call `UploadIngestService._file_upload` to MOVE the original. `_mark_gmail_filed` /
> `_mark_gmail_skipped` now no-op for `drive-upload:` ids. (Split UI is single-attachment-only, so
> uploads only flow through approve/correct.)

### 5. Config (`core/config.py`) ✅ DONE
- `uploads_scan_enabled: bool = False` (off until folders exist on the live Drive).
- `uploads_scan_interval_minutes: int = 15`.
- `client_uploads_folder_name: str = "Client Uploads"`, `rres_uploads_folder_name: str = "RRES Uploads"`.
- Ensure `"Client Uploads"` stays in the fixed Level-2 set (it already is, per the spec) so it's
  auto-created per entity; ensure `RRES Uploads` is in `drive_non_entity_folders` (already present).
> All four settings added. `"Client Uploads"` confirmed in the L2 set; `"RRES Uploads"` confirmed in
> `drive_non_entity_folders`. **Deploy:** set `UPLOADS_SCAN_ENABLED=true` in the server `.env` once
> the upload folders are created/shared on the live Drive.

## Edge cases (must hold)
- **Same file scanned before it's moved** (two ticks overlap): dedup by `drive-upload:<id>` +
  the `processing` state guard prevents double work.
- **File the reviewer hasn't resolved yet**: stays in the upload folder; re-listed each tick but
  skipped (already has a pending ProcessedEmail). No duplicate review items.
- **Unsupported / oversized file**: pdf_service marks it `unsupported` → goes to Needs Review,
  left in place (never silently dropped).
- **Move fails (permissions / file vanished)**: caught; ProcessedEmail marked `failed`; retried by
  the dedicated **upload-retry branch** (NOT the Gmail retry loop, which excludes `drive-upload:%`);
  file stays put.
- **Client drops a duplicate of an already-filed doc**: `ProcessedFile(hash, dest_folder)` dedup —
  recognized as duplicate; original still removed from the upload folder so it doesn't linger.
- **`uploads_scan_enabled=false` / mock mode**: loop is a no-op (like the other gated loops).
- **No "Client Uploads" folder exists yet for an entity**: `find_upload_folders()` simply doesn't
  return one, so there's nothing to scan — correct (clients can only upload to a folder that exists/
  was shared). The per-entity "Client Uploads" folder is created by the normal entity-scaffolding
  (it's in the fixed Level-2 set); the scan never needs to create source folders itself. The
  DESTINATION folder for a filed doc is created on demand by `resolve_target_folder` as usual.

## Smaller notes (from review)
- **Filename date**: the prefix still comes from the document's own date (classifier →
  `filename_date` in `decision_audit`, as with emails); Drive `createdTime` is only the fallback
  when no document date is readable. Make this explicit in `UploadIngestService`.
- **In-place edits** (same Drive file id, new content) are skipped by id-dedup — acceptable; note in
  code comment so it's a known, intentional limitation.
- **Null-safety**: `_contact_hints` (`classifier_service.py:336`) and signal extraction are already
  null-safe (`or ""`), so null sender/subject won't crash — but `test_upload_ingest.py` must assert
  a confident classification with `sender=None, subject=None` to lock the central reuse claim.

## Out of scope
- Guest-login upload UI (client explicitly rejected — wants pre-shared Drive folders, no login).
- The new-entity folder-creation SPEED optimization (separate task).

## Verification
- ✅ `cd backend; .venv\Scripts\python.exe -c "from app.main import app; print('ok')"` — passes.
- ✅ Unit test `tests/test_upload_ingest.py` (`python -m tests.test_upload_ingest`) —
  **all assertions pass**, covering every review fix:
  - confident PDF → moved to correct folder, named `YYYY.MM.DD - <Summary>.pdf`;
  - confident **CSV** → moved, named `YYYY.MM.DD - <Summary>.csv` (extension preserved — Gap 1);
  - `sender=None, subject=None` still classifies confidently (null-safety claim);
  - uncertain → review item created AND file left in place (not moved);
  - re-scan of the same Drive file id → skipped (dedup — "never twice");
  - a doc classified to "Client Uploads"/"RRES Uploads" → blocked as a destination (fix 3);
  - retry-loop query excludes `drive-upload:%` rows (fix 2).
- ✅ `test_multi_entity_filing` + `test_review_file_split` → still pass (no email-path regressions).
- ⏳ **Live smoke (PENDING — needs the folders + `UPLOADS_SCAN_ENABLED=true` on a real Drive):**
  drop a bank statement PDF into a client's "Client Uploads" → within a scan it lands in
  `<that client>/Bank Statements/<Bank> (last4)/YYYY.MM.DD - ... .pdf` and the upload folder is
  empty. Drop an ambiguous file → appears in Needs Review and stays in the upload folder until
  approved. Drop an Airbnb CSV in "RRES Uploads" → AI assigns the client and files it as `... .csv`.

## Edge-case audit — bugs found & fixed (post-implementation review)
- **Dedup duplicated the file in the destination.** On a byte-identical hash hit, the code moved a
  SECOND copy into the destination (where the content already lived). Fixed: it now **trashes** the
  redundant original (new `DriveService.trash_file`, recoverable) and points records at the existing
  filed copy — never a second copy in the destination.
- **Re-hashing a vanished temp file on review-approve.** Dedup hashed `artifact.local_path`, which
  can be gone by approval time. Fixed: dedup now uses the **original file's content hash** (Drive
  `md5Checksum`, else hash the download) persisted in `metadata_json.upload.original_hash` at scan
  time — stable across restarts and correct for the moved original (not the generated PDF).
- **API daily-limit churn.** `ApiLimitReached` was caught by the generic handler → marked `failed`
  → retried immediately, re-spending attempts. Fixed: it's caught separately and parked as
  `waiting_api_limit`; the upload-retry loop now retries `failed` (after 5 min) AND
  `waiting_api_limit` (after 2 h), mirroring the email retry.
- **Poisoned-session failure recording.** The `except` recorded the failure without a rollback,
  which could itself fail on a broken transaction. Fixed: `db.rollback()` first, then record under
  its own try/except.
- **md5 field** added to `list_files_in_folder` so the dedup hash is free when Drive provides it.

### 6. UI: show upload provenance ✅ DONE
Uploads already flowed into the dashboard (they write `ProcessedEmail`/`NeedsReview`/`FilingLog`,
so the Processing count, Needs Review queue, and Activity feed all pick them up). But an upload has
no sender/subject, so it looked like an email with a blank "From". Added a `source` field to the
activity + review API responses (derived from `metadata_json.upload.source_kind`) and a
**"Client Upload" / "RRES Upload"** badge next to the subject (+ a "Source" label replacing the
empty "From") in both the Activity feed and the Needs Review list/detail.

## Files changed (this implementation)
- **new** `app/services/upload_ingest_service.py` — the ingestion service + id helpers.
- **new** `tests/test_upload_ingest.py` — unit test.
- `app/services/drive_service.py` — `list_files_in_folder`, `find_upload_folders`, `trash_file`.
- `app/services/decision_service.py` — `UPLOAD_SOURCE_FOLDERS` destination bar.
- `app/services/classifier_service.py` — prompt: never file INTO an uploads folder.
- `app/services/review_service.py` — `_file_review` dispatcher + Gmail-label guards for uploads.
- `app/main.py` — scan/retry loops + lifespan registration + Gmail-retry exclusion.
- `app/core/config.py` — `uploads_scan_*` + folder-name settings.
- `app/schemas/common.py` + `app/schemas/review.py` — `source` field on the API responses.
- `app/api/routes/activity.py` + `app/api/routes/review.py` — populate `source` from provenance.
- `frontend/.../ActivitySection.jsx` + `ReviewSection.jsx` + `dashboard.css` — source badge/label.
