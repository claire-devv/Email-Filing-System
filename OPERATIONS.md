# Operations runbook

Practical rules distilled from real incidents on this project. Each one exists because it broke
production at least once — read it as "why," not just "what."

## Deploying

**Always use `./deploy.sh`** (repo root), run as the `deploy` user from `/srv/rres/app`:

```bash
cd /srv/rres/app && ./deploy.sh
```

Do not hand-run `git pull` / `npm run build` / `systemctl restart` as separate pasted commands.
Two real incidents came directly from that:

- A multi-line command block was pasted into an SSH session with `exit` in the middle. `exit`
  closed the whole session before the restart command after it could run, silently leaving the
  **old** backend code live for hours while everyone assumed the deploy had worked.
- Some deploys ran as `root`, others as `deploy`. Files pulled/built as `root` are unreadable by
  `deploy` (and vice versa), producing `EACCES` / "insufficient permission to add object to
  repository database" errors on the *next* deploy — not the one that caused it.

`deploy.sh` runs everything in one script (nothing to reorder or split across a session boundary)
and refuses to proceed if the current user can't write the repo (tells you the exact `chown` fix
instead of a cryptic `EACCES`).

**If `deploy.sh` reports the ownership error once:** run the `chown -R deploy:deploy` command it
prints (as root), then re-run `./deploy.sh` as `deploy`. Don't run deploy steps as root again
afterward.

## Verify, don't assume

`deploy.sh`'s smoke test checks response **bodies**, not just HTTP status — this matters. When
`file.rresai.com`'s nginx config had a stale port-80 block still pointing at the old domain, nginx
silently served its **default placeholder page** for plain HTTP requests. That page returns
`200 OK`, so a status-code-only check would have reported success. The script instead greps for
`RRES Dashboard` (frontend) and `"status":"ok"` (backend) in the actual response body.

If you're troubleshooting manually and not using the script, always check the response body, not
just the status code — a 200 doesn't mean "your app," it can mean "nginx's default site."

## Changing domains / nginx config

When Certbot or a manual edit changes `server_name` for a domain, **check every server block that
mentions the old domain**, not just the one you're actively editing. This bit us once: the HTTPS
(443) block was updated to the new domain, but the separate HTTP→HTTPS redirect block (port 80)
still had `server_name <old-domain>`, so plain `http://` requests to the new domain matched no
block and fell through to nginx's default site.

Checklist for any domain change:
1. `grep -n "server_name\|listen" /etc/nginx/sites-available/<file>` — confirm every block (not
   just the 443 one) references the new domain.
2. `sudo nginx -t` — **always as root/sudo**. Running it as a non-root user (e.g. `deploy`)
   fails with a misleading `Permission denied` reading `/etc/letsencrypt/live/...`, because that
   directory is root-only — that's expected for a non-root user and does *not* mean the live
   server is broken. Don't chase that as a real bug.
3. `sudo systemctl reload nginx`
4. Test **both** `http://` and `https://` for the domain from a browser or `curl -I`, not just
   https — the whole class of bug above only shows up on plain HTTP.

## Google OAuth (Drive/Gmail)

If you see `google.auth.exceptions.RefreshError: invalid_client` in `journalctl -u rres-backend`,
the OAuth client secret Google issued no longer matches what's in
`backend/credentials/client_secret.json`. Re-download the current client secret from Google Cloud
Console → APIs & Services → Credentials, replace that file, restart the backend. If the client ID
itself changed (not just the secret), also reconnect via **Settings → Integrations → Reconnect
Google** in the dashboard afterward.

## Large file downloads (Documents page)

The `/documents/{id}/download` route fully buffers the file server-side before responding (see
`backend/app/api/routes/documents.py`) rather than streaming it. This was a deliberate choice
after streaming caused `net::ERR_FAILED` for large files — ambiguous end-of-body framing across
an HTTP/1.0 backend connection, nginx, and an HTTP/2 client. Buffering + an exact `Content-Length`
computed from the real byte count sidesteps that entirely. If you're tempted to switch this back
to a `StreamingResponse` for memory reasons, re-test with an 18MB+ real file end-to-end (not just
`curl -I`) before shipping it — the previous streaming bug looked fine in every log and only
showed up as a browser-side network error.

nginx's proxy timeouts for `api.rresai.com` are set to 300s (`proxy_read_timeout` /
`proxy_send_timeout` / `proxy_connect_timeout`) specifically to give this buffering approach room
to finish fetching large files from Drive before nginx gives up waiting. If they get reverted to
nginx's 60s defaults, large downloads will start failing again.
