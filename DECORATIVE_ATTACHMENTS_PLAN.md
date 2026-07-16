# Plan: decorative/signature images must stop blocking auto-filing

## The three reported symptoms (one root cause)

1. **Emails held for review despite ≥80% confidence** — e.g. "Tax Center Access" at 88%.
2. **"An attachment couldn't be fully read" shown when nothing failed** — the logo was read fine.
3. **Multi-entity "Verify assignments" cluttered with duplicate logo rows** — BD Capital email
   showed 4 identical "BD Capital Logo Image" verify rows next to the 2 real loan statements.

## Root cause (traced, not guessed)

`gmail_service._classify_part` already detects "this image is probably decorative" and tags the
attachment `part_classification_issue="ambiguous_image_part"`. But that tag survives only as an
email-level STRING in `PreparedEmail.issues` (`pdf_service.py:58-59` appends
`"<filename>: ambiguous_image_part"`). It is NOT persisted on the artifact:
`_persist_artifacts` (`processing_service.py:371`) stores `item.issue` — which is only set for
conversion failures/oversize and forces `status="unsupported"` — so a flagged logo lands in
`file_artifacts` indistinguishable from a real document.

Downstream, three places pay for that lost information:

- **`decision_service._multi_entity_plan`** treats the logo as a full peer attachment that must
  resolve to a confident Known entity. A logo rarely does → `every_confident_known=False` →
  `multiple_entities` reason → the whole email (including two perfectly confident loan
  statements) is held. This is symptom 1 and most of symptom 3.
- **`decision_service.validate` lines 175-215**: the "only ambiguous images" deferral re-adds
  `partial_conversion_failure` whenever `not (all_matched and every_confident_known)` — but that
  condition is usually false *because of the logo itself* (circular), or because of an unrelated
  legitimate reason (text-referenced second entity). Either way the reviewer sees "an attachment
  couldn't be fully read" when nothing failed to read. Symptom 2.
- **`ReviewSection.jsx` split panel** renders one Verify row per `artifact_classifications`
  entry; each thread reply re-attaches the same signature image, so one logo = N rows. Symptom 3.

## Design decision (recommended: Option B)

What should happen to an image everyone agrees is decorative?

- **Option A — file it quietly** under the primary entity (Misc.). Nothing dropped, but Drive
  accumulates junk files like `2026.07.08 - BD Capital Logo Image.pdf` ×4.
- **Option B — mark it `internal` (not uploaded standalone). RECOMMENDED.** Nothing is lost:
  the full email including every image is already archived as the combined email PDF in the
  entity's Communications folder. A standalone logo PDF adds zero value and pollutes folders.

Safety rule either way: an image is treated as decorative ONLY when **both** independent signals
agree — the deterministic part-classifier flagged it (`ambiguous_image_part`) **and** Claude,
having actually looked at it, marks it `"decorative": true` in its per-attachment entry. If
either disagrees (e.g. an iPhone screenshot of a lease: heuristic flags it, Claude says it's a
real document), it stays on the normal document path with full confidence gating. Claude alone
can never suppress an attachment the heuristics considered a real document.

## Implementation steps

### 1. Persist the flag to the artifact (`types.py`, `pdf_service.py`, `processing_service.py`)
- Add `ambiguous_image: bool = False` to `PreparedArtifact` (NOT reusing `.issue`, which means
  "unsupported").
- `pdf_service._prepare_attachment`: set it from `attachment.part_classification_issue ==
  "ambiguous_image_part"`.
- `_persist_artifacts`: store `"ambiguous_image": True` in `FileArtifact.metadata_json`
  (status stays "prepared"). No DB migration — JSON column.

### 2. Let Claude judge decorative vs document (`classifier_service.py`)
- `_artifact_prompt_payload`: include `"possibly_decorative": true` for flagged artifacts.
- Prompt: for such attachments, the artifact_summaries entry MUST include
  `"decorative": true|false` — true for signature logos/banners/inline decoration, false for a
  real embedded document (screenshot of a statement, photographed receipt, etc.).
- `_normalize_artifact_summaries`: carry `decorative` through into
  `artifact_classifications[key]`.

### 3. Gate ignores agreed-decorative artifacts (`decision_service.py`)
- Helper `is_decorative(audit, artifact)` = metadata flag AND Claude entry decorative:true.
- `_multi_entity_plan`: skip decorative artifacts exactly like `combined_package`/`email_body`
  (no entity requirement, don't touch `every_confident_known`/`all_matched`).
- Delete the deferred re-add at lines 211-215 and the `only_ambiguous_images` special-casing:
  `ambiguous_image_part` issue strings must NEVER map to `partial_conversion_failure` (nothing
  failed). Real conversion-failure issues keep today's behavior exactly.

### 4. Filing marks decorative artifacts internal (`filing_service.py`) — Option B
- `_should_upload_artifact` (single choke point used by auto-file AND review approve): return
  False for agreed-decorative artifacts → `_mark_internal`. Content remains available in the
  combined email PDF in Communications.
- Verify the review split path (`File each separately`) also flows through this choke point;
  if it routes per-attachment independently, add the same guard there.

### 5. Frontend (`ReviewSection.jsx`)
- Split panel: exclude agreed-decorative entries from the Verify rows; show one muted line:
  "N signature/decorative image(s) — kept inside the archived email PDF, not filed separately."
- No change to the `partial_conversion_failure` copy — step 3 stops it from firing spuriously.

## Expected outcomes on the three reported emails

- **BD Capital**: logos → internal + hidden; the two loan statements are confident Known matches
  → auto-splits and files with NO human review at all.
- **Tax Center Access (both)**: no more bogus "couldn't be read"; still held for review, but for
  the single honest reason — a second client (2020 Frankford) is referenced with no document of
  its own. (Whether THAT should auto-file instead is a separate policy question for the client;
  current hold-for-human behavior is by design.)
- **Any genuinely failed conversion**: still flagged exactly as today.

## Verification

- New unit cases (extend `test_decision_validator` / `test_multi_entity_filing`):
  1. 2 confident Known docs + 2 flagged logos (Claude decorative:true) → `file`, auto-split,
     logos internal, `partial_conversion_failure` NOT in reasons.
  2. Same but Claude decorative:false on one image → that image gated like a normal doc.
  3. additional_entities text-mention without its own doc + a logo → reasons contain
     `multiple_entities` only.
  4. Real conversion-failure issue → `partial_conversion_failure` still present.
  5. Flagged image is the ONLY attachment: decorative:false+confident → files;
     decorative:true → email files as Communications-only (no real attachments), per the
     existing prompt rule.
- Full regression suite: `test_email_artifacts`, `test_review_file_split`, `test_upload_ingest`.
- Live smoke after deploy: re-run one of the held Frankford emails and the BD Capital email
  through reprocess; confirm the outcomes above.

## Rollout

- Backend + frontend, `./deploy.sh`. No migration, no new dependencies.
- Backward compatible: items already in Needs Review keep their stored reasons; only newly
  processed emails get the new behavior. Conservative by construction (both-signals rule).

## Explicitly out of scope

- The "second client referenced but has no document" hold (works as designed; policy question).
- The pending id=95 billing-retry verification (separate thread of work).
