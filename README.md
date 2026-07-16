# RRES — Email Filing System (monorepo)

Automated Gmail → Google Drive filing for Rock RE Services: incoming email is
classified with Claude, rendered to PDF (inline images preserved), and filed into
the correct client/property folder, with a human review queue for low-confidence
items.

## Layout

```
.
├── backend/    FastAPI service (classification, PDF rendering, Drive filing, Gmail watch)
└── frontend/   React + Vite dashboard (review queue, activity, notifications, admin)
```

Each app has its own README with details. This repo is one git repository; the two
apps deploy independently.

## Backend (FastAPI)

Run everything with `backend/` as the working directory (paths in `config.py` and
`alembic.ini` are resolved relative to it).

```powershell
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8088
```

- Config: `backend/.env` (copy from `backend/.env.example`).
- Native rendering libs: WeasyPrint needs the GTK DLLs pointed to by
  `WEASYPRINT_DLL_DIRECTORIES` (already set in `.env`).
- Tests: `cd backend; .venv\Scripts\python.exe -m tests.test_email_artifacts`

## Frontend (React + Vite)

```powershell
cd frontend
npm install
npm run dev      # dev server on http://localhost:5173
npm run build    # production build -> frontend/dist
```

- Config: `frontend/.env` (copy from `frontend/.env.example`); set
  `VITE_API_BASE_URL` to the backend's URL.

## Deployment

- **Backend → Railway** (container + persistent volume; Dockerfile installs GTK via
  apt). Point the Gmail Pub/Sub push subscription at the deployed URL.
- **Frontend → Cloudflare Pages** (static build of `frontend/dist`). Set
  `VITE_API_BASE_URL` to the backend URL and add the Pages domain to the backend's
  `CORS_ORIGINS`.
# Email-Filing-System
# Email-Filing-System
