# RRES Phase 1 Local Backend

Local FastAPI backend for the RRES email filing workflow.

This project replaces the earlier Claude Cowork/JSON-sync prototype with a cleaner backend architecture:

```text
Gmail intake
-> PDF/document preparation
-> Claude final filing decision
-> backend validation
-> Google Drive filing or Needs Review
-> activity, learning, notifications, and review APIs
```

The old Cowork runner is reference only. This backend is a clean Phase 1 implementation.

## What Phase 1 Does

- Fetches unread or specific Gmail messages from the filing inbox.
- Downloads email body and attachments.
- Creates PDF filing artifacts with source cover pages.
- Sends email context and scanned PDFs to Claude when enabled.
- Validates Claude output with deterministic backend rules.
- Auto-files only valid, high-confidence known-entity documents.
- Routes uncertain, unknown, unsupported, oversized, or risky items to Needs Review.
- Lets reviewers approve, correct, or reject items through API endpoints.
- Stores learned sender/domain/keyword mappings from reviewer corrections.
- Uploads/moves/renames files in Google Drive.
- Tracks duplicate files using file hash plus Drive folder.
- Exposes dashboard-ready APIs for activity, review queue, entities, artifacts, and notification counts.

## Project Structure

```text
app/
  api/routes/             FastAPI route modules
  core/                   settings and logging
  db/                     SQLAlchemy models/session
  schemas/                API response/request schemas
  scripts/                local setup and test scripts
  services/               Gmail, Drive, Claude, filing, review, validator logic
  utils/                  small file/date/hash helpers
data/
  folder_structure.json   local copy of the client folder rulebook
migrations/               Alembic scaffold
runtime/                  local DB/artifacts/logs, ignored by git
credentials/              OAuth secrets/tokens, ignored by git
```

## Core Rules

- `Needs Review` is the operational fallback.
- `Ask Client - Closed` is a real client Level 2 folder, not a fallback.
- Claude action is advisory; backend validator is final authority.
- Entity matching is exact in Phase 1.
- Unknown entities go to Needs Review.
- Reviewer `Correct` can create a new entity folder and all fixed Level 2 folders.
- Final filenames follow:

```text
YYYY.MM.DD - Summary including entity/property/entity reference.pdf
```

## Setup

Run from the project root:

```powershell
cd "C:\Users\ataur\Documents\Claude Cowork\Matt's Project"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Initialize the local SQLite database:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.init_db
```

Start the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8088
```

Open Swagger:

```text
http://127.0.0.1:8088/docs
```

## Environment Variables

Edit `.env` after copying `.env.example`.

Safe local/mock mode:

```env
CLASSIFIER_MODE=mock
ENABLE_REAL_CLAUDE=false
ENABLE_REAL_GOOGLE=false
```

Real API mode:

```env
CLASSIFIER_MODE=claude
ENABLE_REAL_CLAUDE=true
ENABLE_REAL_GOOGLE=true
ANTHROPIC_API_KEY=your_anthropic_key
CLAUDE_MODEL=claude-sonnet-4-6
DRIVE_ROOT_ID=your_drive_root_id
DRIVE_ROOT_NAME=RRES - Books
```

Recommended paid API safety while testing:

```env
CLAUDE_DAILY_CALL_LIMIT=3
CLAUDE_MAX_CALLS_PER_PROCESS=1
PROCESS_UNREAD_MAX_LIMIT=1
```

Admin repair endpoint requires:

```env
ADMIN_API_KEY=choose-a-local-admin-key
```

Never commit `.env`, OAuth tokens, API keys, local DB files, or generated PDFs.

## Google Gmail and Drive Setup

1. Create or select a Google Cloud project.
2. Enable:
   - Gmail API
   - Google Drive API
3. Create an OAuth Client ID.
4. For the smooth API/dashboard flow, use **Web application** OAuth.
5. Add this authorized redirect URI:

```text
http://127.0.0.1:8088/auth/google/callback
```

If you change `GOOGLE_OAUTH_REDIRECT_URI`, add that exact value in Google Cloud too.

5. Download the OAuth JSON.
6. Save it as:

```text
credentials/client_secret.json
```

7. Start the backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8088
```

8. Call:

```http
GET /auth/google/connect
X-RRES-Admin-Key: your-admin-key
```

Open the returned `auth_url`, approve Google access, and Google will return to:

```text
http://127.0.0.1:8088/auth/google/callback
```

9. Sign in with the Gmail/Drive account that can access:
   - the filing inbox
   - the target Google Drive root folder/shared drive

10. Save and validate the target Drive root folder from a folder URL:

```http
PUT /auth/google/drive-root
X-RRES-Admin-Key: your-admin-key
Content-Type: application/json

{
  "folder_url_or_id": "https://drive.google.com/drive/folders/0ABtkmhQfZx63Uk9PVA",
  "drive_root_name": "RRES - File Test_Claire",
  "validate_access": true
}
```

Check setup status:

```http
GET /auth/google/status
X-RRES-Admin-Key: your-admin-key
```

The generated token is stored locally at:

```text
credentials/token.json
```

This token is ignored by git.

Fallback CLI auth is still available for local developer setup:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.auth_google
```

## Google Drive Root ID

For a Drive folder URL like:

```text
https://drive.google.com/drive/folders/0ABtkmhQfZx63Uk9PVA
```

Set:

```env
DRIVE_ROOT_ID=0ABtkmhQfZx63Uk9PVA
```

## Anthropic Claude Setup

Set:

```env
ANTHROPIC_API_KEY=your_key
CLAUDE_MODEL=claude-sonnet-4-6
```

Test the key with a tiny call:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.test_claude_key
```

If you see `invalid x-api-key`, confirm the key in `.env` is the same key that works in your terminal. The app forces local `.env` to win over stale PowerShell environment variables.

## Local Non-Paid Checks

These do not call Claude, Gmail, or Drive:

```powershell
.\.venv\Scripts\python.exe -m compileall app -q
.\.venv\Scripts\python.exe -m tests.test_decision_validator
```

The validator test covers:

- `0.82` confidence normalized to `82`
- low confidence routed to Needs Review
- Claude `needs_review` action respected
- unsafe reject routed to Needs Review
- clear spam/social reject accepted
- partial entity routed to Needs Review
- Level 2/Level 3 folder rules
- document date fallback
- invalid Claude JSON routed to Needs Review

## Main API Endpoints

Email processing:

```text
POST /emails/{gmail_message_id}/process
POST /emails/process-unread
```

Activity:

```text
GET /activity
GET /activity/{id}/files
```

Review:

```text
GET  /review/items
POST /review/items/{id}/approve
POST /review/items/{id}/correct
POST /review/items/{id}/reject
```

Entities:

```text
GET  /entities
POST /entities
```

Notifications:

```text
GET /notifications/counts
```

Folder rules:

```text
GET  /admin/folder-rules
POST /admin/folder-rules/reload
PUT  /admin/folder-rules
```

Admin repair:

```text
POST /admin/repair/review/{review_id}
```

Admin repair requires this header:

```text
X-RRES-Admin-Key: your-admin-key
```

## Testing A Real Email

1. Keep API limits low in `.env`:

```env
CLAUDE_DAILY_CALL_LIMIT=3
PROCESS_UNREAD_MAX_LIMIT=1
```

2. Restart FastAPI after editing `.env`.

3. In Swagger, run:

```text
POST /emails/process-unread
```

Body:

```json
{
  "limit": 1,
  "newer_than_minutes": 60
}
```

4. Check:

```text
GET /activity
GET /review/items
GET /notifications/counts
```

## Optional Gmail Pub/Sub Trigger

The app can use Google Pub/Sub as a push trigger. Pub/Sub does not send full email data; it sends a Gmail `historyId`, and the backend uses Gmail API to fetch the actual new message IDs.

Setup summary:

1. Create a Pub/Sub topic in the client Google Cloud project.
   - The topic project must match the Google Cloud project that owns the OAuth client in `credentials/client_secret.json`.
2. Grant `gmail-api-push@system.gserviceaccount.com` the `Pub/Sub Publisher` role on that topic.
3. Create a push subscription to:

```text
https://YOUR_PUBLIC_BACKEND_URL/webhooks/gmail/pubsub
```

For local ngrok testing:

```text
https://YOUR_NGROK_DOMAIN/webhooks/gmail/pubsub
```

4. Start the Gmail watch:

```http
POST /admin/gmail/watch/start
X-RRES-Admin-Key: your-admin-key

{
  "topic_name": "projects/YOUR_PROJECT_ID/topics/rres-gmail-events",
  "label_ids": ["INBOX"],
  "label_filter_behavior": "INCLUDE"
}
```

5. Check:

```http
GET /admin/gmail/watch/status
X-RRES-Admin-Key: your-admin-key
```

Gmail watches must be renewed before expiration. See `SETUP_REAL_APIS.md` for the full flow.

## Review Workflow

Approve:

- Accepts Claude proposal.
- Backend validates the proposal.
- If valid, files and renames documents.
- If invalid, returns validation error and reviewer should use Correct.

Correct:

- Reviewer supplies entity, Level 2, Level 3, summary, and optional document date.
- Backend validates corrected decision.
- If entity is new and valid, backend creates the entity folder and all fixed Level 2 folders.
- Backend moves/renames documents and records learned mappings.

Reject:

- Marks item rejected.
- Does not upload to final client folders.
- Shows in Activity.
- Removes it from active Needs Review.

## Folder Rulebook

The local rulebook is:

```text
data/folder_structure.json
```

The app loads it into cache and stores a database snapshot. It does not reread the file for every email.

Rulebook behavior:

- Startup attempts to load current rulebook source.
- If unavailable, it uses the last known good DB cache.
- If there is no valid file or DB cache, processing fails fast.
- Malformed reload keeps the existing cache and logs `system_error`.
- Cache refresh only happens through:

```text
POST /admin/folder-rules/reload
```

## GitHub Push Commands

Use these from the project root. This assumes the remote repo is empty.

```powershell
cd "C:\Users\ataur\Documents\Claude Cowork\Matt's Project"

git init -b main
git config user.name "ataur-rehman"
git config user.email "your-github-email@example.com"

git remote add origin https://github.com/malindtech/Claude-Project.git
git status --short
git add .
git status --short
git commit -m "Initial RRES phase 1 backend"
git push -u origin main
```

If Git says the repo already exists locally but branch is `master`, run:

```powershell
git branch -M main
git remote remove origin
git remote add origin https://github.com/malindtech/Claude-Project.git
git push -u origin main
```

If GitHub asks for credentials, use your GitHub username and a personal access token, not your account password.

## Important Security Notes

- Rotate any API key that was pasted into chat, terminal output, or screenshots.
- Do not commit `.env`.
- Do not commit `credentials/client_secret.json`.
- Do not commit `credentials/token.json`.
- Do not commit `runtime/`.
- Do not commit local PDFs or generated artifacts.
