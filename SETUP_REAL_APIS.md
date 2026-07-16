# Real API Setup For Phase 1 Local Testing

This backend can use real Gmail, Drive, and Claude APIs. Because Claude is paid, real calls are protected by explicit flags and a daily call limit.

## 1. Create `.env`

Copy:

```powershell
Copy-Item .env.example .env
```

For the first real test, keep limits low:

```text
CLASSIFIER_MODE=claude
ENABLE_REAL_CLAUDE=true
CLAUDE_DAILY_CALL_LIMIT=3
CLAUDE_MAX_CALLS_PER_PROCESS=1
CLAUDE_MAX_PROMPT_CHARS=12000

ENABLE_REAL_GOOGLE=true
PROCESS_UNREAD_MAX_LIMIT=1
```

Add:

```text
ANTHROPIC_API_KEY=your_key_here
DRIVE_ROOT_ID=your_drive_folder_or_shared_drive_id
DRIVE_ROOT_NAME=RRES - Books
```

Do not paste API keys into chat.
If a key was ever pasted into chat or terminal output, rotate it in the Anthropic Console before testing.

Test the Claude key before processing emails:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.test_claude_key
```

This makes one tiny Claude call. Only run it after you are sure the key is correct.

## 2. Connect Client Gmail Properly

For local Phase 1, use the API/dashboard OAuth flow:

1. Open Google Cloud Console.
2. Create/select a project for this automation.
3. Enable APIs:
   - Gmail API
   - Google Drive API
4. Configure OAuth consent screen.
5. Add the client/test Gmail as a test user if the app is in testing mode.
6. Create OAuth Client ID:
   - Application type: Web application
   - Authorized redirect URI:

```text
http://127.0.0.1:8088/auth/google/callback
```

This must exactly match `GOOGLE_OAUTH_REDIRECT_URI` in `.env`.

7. Download the OAuth JSON.
8. Save it here:

```text
credentials/client_secret.json
```

9. Start the backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8088
```

10. In Swagger or your dashboard, call:

```http
GET /auth/google/connect
X-RRES-Admin-Key: your-admin-key
```

11. Open the returned `auth_url` and sign in with the Gmail account that owns/receives `file@rockreservices.com` or has delegated access.

The token is saved to:

```text
credentials/token.json
```

12. Save and validate the Drive root folder:

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

This writes `DRIVE_ROOT_ID` and `DRIVE_ROOT_NAME` into `.env` so future filing runs use that Drive folder.

13. Confirm status:

```http
GET /auth/google/status
X-RRES-Admin-Key: your-admin-key
```

Fallback local CLI auth is still available if you use a Desktop OAuth client:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.auth_google
```

## 3. Initialize DB

```powershell
.\.venv\Scripts\python.exe -m app.scripts.init_db
```

## 4. Start Backend

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8088
```

Open:

```text
http://127.0.0.1:8088/docs
```

## 5. Safe Real Test Order

Recommended first test:

1. Send exactly one unread filing email to the connected Gmail.
2. Use:

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

Because `PROCESS_UNREAD_MAX_LIMIT=1`, the backend will process only one unread email even if more exist.

## 6. Claude Usage Safety

Check usage:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.show_usage
```

Reset today's local Claude counter only if you intentionally want another batch:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.reset_claude_usage
```

If the daily limit is reached, the backend blocks further Claude calls until you raise `CLAUDE_DAILY_CALL_LIMIT` or reset the local counter.

## 7. Drive Root ID

From a Drive folder URL:

```text
https://drive.google.com/drive/folders/FOLDER_ID_HERE
```

Use:

```text
DRIVE_ROOT_ID=FOLDER_ID_HERE
```

For a Shared Drive root, the ID may start with `0A...`; that is okay as long as the OAuth account has access.

## 8. Optional Gmail Pub/Sub Push Setup

Pub/Sub is only a trigger. Gmail sends `emailAddress` and `historyId`; the backend then calls Gmail `history.list` and processes the real message IDs.

1. In Google Cloud, enable:
   - Gmail API
   - Cloud Pub/Sub API

2. Create a Pub/Sub topic, for example:

```text
projects/YOUR_PROJECT_ID/topics/rres-gmail-events
```

Important: `YOUR_PROJECT_ID` must be the same Google Cloud project that owns the OAuth client in `credentials/client_secret.json`. Gmail rejects `users.watch` if the topic is in a different project.

3. On the topic permissions, grant this principal:

```text
gmail-api-push@system.gserviceaccount.com
```

Role:

```text
Pub/Sub Publisher
```

4. Create a push subscription pointed at your backend:

```text
https://YOUR_PUBLIC_BACKEND_URL/webhooks/gmail/pubsub
```

For local testing, run ngrok first:

```powershell
ngrok http 8088
```

Then use:

```text
https://YOUR_NGROK_DOMAIN/webhooks/gmail/pubsub
```

5. Optionally set the topic in `.env`:

```env
GMAIL_PUBSUB_TOPIC_NAME=projects/YOUR_PROJECT_ID/topics/rres-gmail-events
GMAIL_PUBSUB_LABEL_IDS=["INBOX"]
GMAIL_PUBSUB_LABEL_FILTER_BEHAVIOR=INCLUDE
```

6. Start the Gmail watch:

```http
POST /admin/gmail/watch/start
X-RRES-Admin-Key: your-admin-key
Content-Type: application/json

{
  "topic_name": "projects/YOUR_PROJECT_ID/topics/rres-gmail-events",
  "label_ids": ["INBOX"],
  "label_filter_behavior": "INCLUDE"
}
```

If `GMAIL_PUBSUB_TOPIC_NAME` is set, the body can be:

```json
{}
```

7. Check status:

```http
GET /admin/gmail/watch/status
X-RRES-Admin-Key: your-admin-key
```

8. Renew the watch daily:

```http
POST /admin/gmail/watch/renew
X-RRES-Admin-Key: your-admin-key
Content-Type: application/json

{}
```

Gmail watches expire, so renewal must be scheduled before expiration.

9. Stop the watch if needed:

```http
POST /admin/gmail/watch/stop
X-RRES-Admin-Key: your-admin-key
```

Keep `/emails/process-unread` as a backup job, because Gmail push notifications can occasionally be delayed or dropped.
