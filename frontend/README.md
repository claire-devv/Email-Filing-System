# RRES Dashboard (frontend)

React + Vite SPA for the RRES email-filing system: dashboard overview, review queue,
activity log, notifications, and admin panel.

## Setup

```bash
cp .env.example .env     # set VITE_API_BASE_URL to your backend
npm install
npm run dev              # http://localhost:5173
npm run build            # production build -> dist/
```

## Configuration

- **`VITE_API_BASE_URL`** — base URL of the backend API (no trailing slash). Every
  request goes through [`src/api/client.js`](src/api/client.js), which prefixes this
  base and attaches the auth token.

## Auth

Login ([`src/pages/login/Login.jsx`](src/pages/login/Login.jsx)) posts to
`${VITE_API_BASE_URL}/auth/login` and stores the returned bearer token
([`src/utils/tokenUtils.js`](src/utils/tokenUtils.js)). Protected API calls send it
as `Authorization: Bearer <token>`; a `401` clears the token and returns to login.
