# Deploying to Vercel + Railway + Supabase

The one-process demo (`.\start.ps1`) serves the UI and the API from the same
origin. A production deployment splits them:

```
Browser
   |  https://<your-app>.vercel.app          static export, no Node at runtime
   v
Vercel
   |  https://<your-api>.up.railway.app      fetch() with a bearer token
   v
Railway (FastAPI, one container)
   |  postgresql://...pooler.supabase.com    psycopg2, this app's own pool
   v
Supabase PostgreSQL
```

**Nothing about the application changed to make this work.** The pipeline, the
rule engine, the review workflow, the portal, the analytics and the assistant
are untouched. What changed is four things that were only ever correct while
one process served both halves — they are listed in "What this required" at the
bottom, so a reviewer can check the claim rather than take it.

---

## 0. What you need before starting

| | Why |
|---|---|
| A Supabase project | the database |
| A Railway account | the API container |
| A Vercel account | the static UI |
| A Google Cloud project | only if you want Gmail ingestion / rejection emails |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | only if you want LLM extraction; the regex route works without either |

Everything below is done in that order, because each step needs the URL the
previous one produced.

---

## 1. Supabase — the database

### 1.1 Get the connection string

Supabase dashboard → **Project Settings → Database → Connection string**. You
are offered three. **Take the Session pooler**:

| Variant | Host | Use it? |
|---|---|---|
| Direct connection | `db.<ref>.supabase.co:5432` | Only if your project has the IPv4 add-on — this host is IPv6-only otherwise, and Railway's egress may not reach it. |
| **Session pooler** | `aws-0-<region>.pooler.supabase.com:5432` | **Yes.** IPv4, and behaves like a real session. |
| Transaction pooler | `...pooler.supabase.com:6543` | **No. It will break this app.** |

**Why the transaction pooler breaks it**, since this is not a preference:
`backend/storage.py` runs its own `ThreadedConnectionPool` and issues
`SET search_path` **once** per borrowed connection, then runs several
statements against it before returning it (`get_conn()` / `write_txn()`).
PgBouncer's transaction mode recycles the underlying server connection between
statements, so that session-scoped `SET` is not guaranteed to still apply — and
`SELECT ... FOR UPDATE`, which is how every concurrency guarantee in this
application is enforced, needs a transaction that spans statements.

Keep the `?sslmode=require` Supabase includes. It refuses plaintext anyway.

### 1.2 Initialize the schema

**There is no migration tool and you do not need one.** `storage.init_db()`
runs on every FastAPI startup and is idempotent: it creates any missing table,
adds any missing column (`_ensure_columns`), creates the indexes, and reloads
`purchase_orders`, `vendors` and `trusted_email_senders` from `data/*.json`.

So: **set `DATABASE_URL` on Railway and start the service.** That is the whole
initialization. Nothing drops or truncates anything — `runs` and everything
derived from it are never touched by the seed reload, so deploying over an
existing database preserves its history.

To do it before the first deploy instead, from a checkout:

```powershell
$env:DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
.\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'backend'); import storage; storage.init_db(); print('schema ready')"
```

Expect afterwards: 14 tables, ~41 indexes, 9 purchase orders, 8 vendors, 8
trusted senders. (`extraction_quota` is created lazily by `quota.py` on first
extraction, so it may be absent until something is processed — that reads as
"nothing has been extracted yet", not as a failure.)

### 1.3 Document storage — do not skip this

Uploaded invoice PDFs do **not** go in Postgres. `documents` holds metadata and
an opaque key; the bytes live behind `backend/documents.py`.

**A Railway container's filesystem does not survive a deploy.** With the default
`DOCUMENT_STORE_BACKEND=local`, every uploaded PDF is gone at the next restart.
The `documents` row survives (it is in Postgres), so the run still opens and the
audit trail is intact — but the download 404s and the audit report has no source
document. Nothing warns you, because from the application's side the write
succeeded.

The application already has the answer and it is a config switch, not a code
change. Supabase Storage is S3-compatible, which keeps everything on one vendor:

1. Supabase → **Storage** → new bucket, e.g. `invoice-documents`, **private**.
2. Supabase → **Project Settings → Storage → S3 connection** — copy the
   endpoint and region.
3. Supabase → **Storage → S3 Access Keys** — create one; copy both halves.

Then on Railway:

```
DOCUMENT_STORE_BACKEND=s3
DOCUMENT_S3_BUCKET=invoice-documents
DOCUMENT_S3_ENDPOINT_URL=https://<project-ref>.storage.supabase.co/storage/v1/s3
DOCUMENT_S3_REGION=<your project region>
AWS_ACCESS_KEY_ID=<S3 access key id>
AWS_SECRET_ACCESS_KEY=<S3 secret access key>
```

`boto3` is already installed in the container image. `AWS_*` are read by boto3
itself, not by this application.

If you deliberately accept losing PDFs on restart — a short-lived demo, say —
leave the default and know that is the trade.

---

## 2. Railway — the API

### 2.1 Create the service

New Project → **Deploy from GitHub repo** → this repository. Railway reads
`railway.json` at the root, which selects the `Dockerfile` build and sets
`/api/health` as the healthcheck.

The image deliberately contains **no frontend**. `main.py` detects that
`frontend-next/out/` is absent and serves the API alone; `GET /` then answers
with a small liveness document instead of the app shell.

### 2.2 Build the user store first

`APP_ENV=production` **refuses to start** while the shipped demo accounts are
present, because their passwords are published in this repository. That refusal
is correct. Generate a real store locally — it prompts for each password and
stores nothing:

```powershell
.\venv\Scripts\python.exe scripts\make_user_store.py generate `
    --user alice:admin --user bob:reviewer --user carol:viewer
```

Roles: `viewer`, `analyst`, `reviewer`, `admin`, and `client` /
`client_readonly` for suppliers (those two also prompt for the vendor binding).

It prints one line of JSON. Paste it into Railway as `AUTH_USERS_JSON`. The
container writes it to `/tmp/users.json` at startup
(`scripts/start-backend.sh`) and points `AUTH_USERS_FILE` at it — a container
has nowhere durable to keep a file, and committing password hashes or baking
them into an image are both worse.

### 2.3 Variables

**Required:**

```
APP_ENV=production
DATABASE_URL=postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
AUTH_SECRET=<python -c "import secrets;print(secrets.token_urlsafe(48))">
AUTH_USERS_JSON=<the line from 2.2>
CORS_ORIGINS=https://<your-app>.vercel.app
FRONTEND_ORIGIN=https://<your-app>.vercel.app
TRUST_PROXY_HEADERS=1
```

`CORS_ORIGINS` and `FRONTEND_ORIGIN` are **not** duplicates: the first decides
which browser origin may call this API, the second decides where the Gmail
OAuth callback sends the administrator's browser afterwards. They happen to
hold the same value in this topology.

`TRUST_PROXY_HEADERS=1` matters more than it looks: without it, per-IP rate
limiting sees Railway's proxy as the client and counts every user in the world
as one address.

**Optional, and only if you want the feature:**

```
GROQ_API_KEY=...                  LLM text route (falls back to regex without it)
GEMINI_API_KEY=...                vision route for scanned PDFs (held for review without it)
DOCUMENT_STORE_BACKEND=s3         + the four S3 variables from 1.3
GOOGLE_OAUTH_CLIENT_ID=...        Gmail — see section 4
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=https://<your-api>.up.railway.app/api/email/oauth/gmail/callback
GMAIL_OAUTH_SCOPES=https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send
CORS_ORIGIN_REGEX=^https://<your-app>-[a-z0-9-]+-<team>\.vercel\.app$    preview builds only
```

**Never** set `CORS_ORIGINS=*`. The production start-up check refuses it, and it
also refuses a `CORS_ORIGIN_REGEX` loose enough to be a wildcard in disguise —
it must be anchored with `^` and `$`, and it is actually run against origins it
must not match.

### 2.4 Deploy, then get the domain

Railway → Settings → Networking → **Generate Domain**. That gives you
`https://<something>.up.railway.app`, which is the value you need in section 3
and section 4.

Verify:

```bash
curl https://<your-api>.up.railway.app/api/health          # {"status":"ok"}
curl https://<your-api>.up.railway.app/                    # the API-only document
```

If the service refuses to start, the log says exactly which setting is wrong —
that is `auth.enforce_production_config()` doing its job, not a crash.

---

## 3. Vercel — the UI

### 3.1 Create the project

New Project → import this repository → **Root Directory: `frontend-next`**.
That is the one setting Vercel cannot infer, and it cannot be set from a file.
Framework preset is detected as Next.js; `frontend-next/vercel.json` supplies
the build command and the security headers.

The build is a **static export** (`output: "export"` in `next.config.mjs`) — no
Node process at runtime, which is unchanged from how this UI has always been
built.

### 3.2 The one variable

```
NEXT_PUBLIC_API_BASE_URL=https://<your-api>.up.railway.app
```

Origin only — no trailing slash, no path. Set it for **Production**, and for
Preview too if you use preview deployments.

`NEXT_PUBLIC_*` is compiled into the browser bundle by design, so this must be
a public origin and nothing else. **No key, no token and no secret goes into a
Vercel variable** — the API keys, the database URL, the JWT secret and the
Gmail refresh token are all server-side on Railway and never leave it.

It is a **build-time** value: changing it requires a redeploy, not a restart.

### 3.3 Content-Security-Policy

`frontend-next/vercel.json` sets `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Cross-Origin-Opener-Policy` and `Permissions-Policy` — the
headers FastAPI used to add when it served the HTML itself.

It deliberately sets **no CSP**, because a correct one names your API origin and
therefore cannot be committed. Add this to the `/(.*)` header block once you
know the Railway domain:

```json
{
  "key": "Content-Security-Policy",
  "value": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https://<your-api>.up.railway.app; object-src 'self' blob:; frame-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
}
```

`connect-src` **must** list the API origin or every request is blocked.
`'unsafe-inline'` for scripts is the existing, documented limitation of a static
export with an inline theme bootstrap — the policy is attack-surface reduction,
not XSS immunity.

---

## 4. Gmail OAuth

### 4.1 The callback route already exists

`GET /api/email/oauth/gmail/callback` — do not invent another one. In
production that is:

```
https://<your-api>.up.railway.app/api/email/oauth/gmail/callback
```

It lives on **Railway**, not Vercel: Google redirects the browser to the server
that holds the client secret and does the token exchange. After the exchange
the server sends the browser on to `FRONTEND_ORIGIN` with a one-word outcome
(`/?gmail=connected`), which is why section 2.3 sets it.

Google requires HTTPS for any redirect URI that is not `localhost`. Railway's
generated domain is HTTPS, so this is satisfied.

### 4.2 Google Cloud setup

1. **APIs & Services → Library → Gmail API → Enable.**
2. **OAuth consent screen** — *Internal* if the mailbox is on a Google
   Workspace domain you control (no verification needed); *External* otherwise,
   which needs Google's review before anyone outside your test users can
   connect. Add only the scopes in 4.3.
3. **Credentials → Create credentials → OAuth client ID → Web application.**
4. **Authorised redirect URIs** — paste the URL above, character for character.
   Google matches it exactly; a trailing slash is a different URI.
5. Copy the client ID and secret into Railway.

### 4.3 Scopes — exactly what this application requests

| Scope | What for | Default? |
|---|---|---|
| `.../auth/gmail.readonly` | ingestion: read messages, download attachments | **yes** |
| `.../auth/gmail.send` | send-only; the rejection-email feature | opt in |
| `.../auth/gmail.modify` | alternative to readonly, if you want ingested mail marked read | opt in |

`https://mail.google.com/`, `gmail.compose` and both settings scopes are
**refused by configuration** — `config.gmail_scopes()` raises rather than
requesting them, so a deployment cannot ask a customer for delete authority
over their mailbox.

For ingestion only, leave `GMAIL_OAUTH_SCOPES` unset. To also send rejection
emails, set it to both, space-separated:

```
GMAIL_OAUTH_SCOPES=https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send
```

`gmail.send` alone is refused — there would be nothing to poll.

### 4.4 Does an existing connection need to reconnect?

**Yes — reconnect once after deploying, for two independent reasons.**

1. **The stored refresh token cannot be decrypted with a new `AUTH_SECRET`.**
   The token is encrypted at rest with a Fernet key derived by HKDF from
   `AUTH_SECRET`. Production has a different secret from your laptop, so the
   stored credential is unreadable there. It fails closed and reports itself;
   the fix is one click.
2. **`gmail.send` is a new scope.** Google fixes a token's scopes at the moment
   of consent, so a mailbox connected under `gmail.readonly` does not silently
   gain the ability to send. `oauth_google.can_send()` checks the **live stored
   connection's** granted scopes on every attempt, so sending is refused with a
   clear reason until someone re-consents.

*(In this repository right now, the connected mailbox
`adityasingh343434@gmail.com` holds `gmail.readonly` only — so rejection emails
would be refused even locally until it is reconnected with the send scope.)*

To reconnect: sign in as an administrator → **Email integration** →
**Disconnect**, then **Connect**. The consent screen will list whichever scopes
`GMAIL_OAUTH_SCOPES` names.

---

## 5. Environment variables, by where they live

**Vercel (public, compiled into the browser bundle):**

| Variable | |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | the Railway origin. The only one. |

**Railway — server-only. None of these ever reaches a browser.**

| Group | Variables |
|---|---|
| Database | `DATABASE_URL` |
| Security | `APP_ENV`, `AUTH_SECRET`, `AUTH_USERS_JSON`, `AUTH_USERS_FILE`, `AUTH_TOKEN_TTL_MINUTES`, `AUTH_ISSUER` |
| Cross-origin | `CORS_ORIGINS`, `CORS_ORIGIN_REGEX`, `FRONTEND_ORIGIN`, `TRUST_PROXY_HEADERS` |
| AI providers | `GROQ_API_KEY`, `GEMINI_API_KEY` |
| Gmail | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `GMAIL_OAUTH_SCOPES`, `GMAIL_SEARCH_QUERY`, `GMAIL_BACKFILL_DAYS`, `EMAIL_INGEST_ENABLED`, `EMAIL_POLL_SECONDS` |
| Documents | `DOCUMENT_STORE_BACKEND`, `DOCUMENT_S3_*`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| Limits | `RATE_LIMIT_*`, `DAILY_QUOTA_*` |
| Headers | `SECURITY_HEADERS_ENABLED`, `HSTS_MAX_AGE_SECONDS`, `CONTENT_SECURITY_POLICY` |

`.env.example` documents every one of these in full. `.env` itself is gitignored
and is excluded from the container image by `.dockerignore`.

---

## 6. Verifying the deployment

```bash
API=https://<your-api>.up.railway.app
UI=https://<your-app>.vercel.app

curl -s $API/api/health                                   # {"status":"ok"}
curl -sI $UI | grep -i x-frame-options                    # DENY

# CORS: the browser origin is allowed, an arbitrary one is not
curl -sI -H "Origin: $UI" $API/api/health | grep -i access-control-allow-origin
curl -sI -H "Origin: https://evil.example" $API/api/health | grep -i access-control-allow-origin   # nothing

# Auth end to end
TOKEN=$(curl -s -X POST $API/api/auth/token \
  -d "username=alice&password=..." | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s $API/api/auth/me -H "Authorization: Bearer $TOKEN"
curl -s $API/api/runs    -H "Authorization: Bearer $TOKEN"
```

In the browser, with devtools open on the Network tab, confirm requests go to
the Railway host and **not** to `localhost`. Then walk: sign in → Overview →
Process invoice → pipeline stages stream → invoice detail → decision → review →
audit trail → PDF and CSV audit export → Purchase orders → Approved vendors →
Analytics → Email integration → Email queue → Assistant → sign out and in as a
supplier account → language picker → dark mode.

Two things worth watching specifically, because they are the ones a split
deployment breaks quietly:

* **the SSE stage stream** on Process invoice — stages should appear one at a
  time, not all at once at the end;
* **the downloaded filename** of an audit report — it should be the
  server-chosen name, which only works because `Content-Disposition` is now in
  the CORS expose list.

---

## 7. What this required (the complete list)

Four things were only ever correct while one process served both halves. Each
is unchanged in the single-origin case, so the local demo and the test suite
behave exactly as before.

1. **The static mount is now conditional** (`backend/main.py`).
   `StaticFiles(directory=...)` raises at import for a missing directory, and
   `frontend-next/out/` is a gitignored build artifact — so a backend-only
   image would have failed to start for want of a UI it was never meant to
   serve. When `out/` is present it is served exactly as before.

2. **The frontend learned where the API is** (`frontend-next/lib/api.ts`).
   Every call site already wrote a relative `/api/...` path and funnelled
   through `apiFetch`, so this is one new helper, `apiUrl()`, and two call
   sites. Empty `NEXT_PUBLIC_API_BASE_URL` means same-origin, which is the
   default and the previous behaviour.

3. **CORS gained `expose_headers`, and optionally a preview-origin pattern**
   (`backend/main.py`, `backend/config.py`, `backend/auth.py`). A cross-origin
   browser hands JavaScript only seven safelisted response headers, and
   `Content-Disposition` is not one — so audit-report and document downloads
   would have silently lost their server-chosen filenames. The regex is empty
   by default and a production start refuses a loose one.

4. **The Gmail callback can redirect to the UI's origin**
   (`backend/main.py`, `backend/config.py`). Google redirects a *browser* to the
   API host; a relative `/` would strand the administrator there. The
   destination comes from server configuration only — never from the request —
   and is validated to `scheme://host[:port]`, so there is still no open
   redirect and the outcome word still comes from a closed set.

Plus the deployment plumbing, which contains no application logic:
`Dockerfile`, `.dockerignore`, `railway.json`, `frontend-next/vercel.json`,
`scripts/start-backend.sh`, `scripts/make_user_store.py`, and `.gitattributes`
pinning `*.sh` and `Dockerfile` to LF.

---

## 8. Limitations you are deploying with

These are pre-existing and documented in `CLAUDE.md`; the split makes some of
them matter more.

1. **Rate limits are per process.** One replica is configured for that reason.
   Raising `numReplicas` multiplies every limit and starts a second email
   poller. The poller stays correct (idempotency is a `UNIQUE` constraint, not
   coordination), the limits do not.
2. **The password grant is still the token issuer.** No MFA, no password
   policy, no rotation. `auth.py` is built to be swapped for an identity
   provider; until then, this is what stands in front of an internet-facing API.
3. **A deleted user's outstanding token stays valid until it expires.** To
   revoke, set `"disabled": true` on the record — do not merely delete it.
4. **`script-src 'unsafe-inline'`** in the CSP, forced by the static export.
5. **No authentication audit log.** Sign-ins, failures and rate-limit trips go
   to stderr — on Railway, to the deploy log — not to a queryable table.
6. **No dependency-vulnerability scanning** is wired into this repository.
7. **`AUTH_SECRET` rotation** signs everyone out *and* makes the stored Gmail
   credential undecryptable. Both fail closed; the Gmail half needs a reconnect.
8. **Ingestion polls, it does not receive push.** Worst-case latency is one
   `EMAIL_POLL_SECONDS` interval.
9. **Supabase free-tier connection limits.** This app opens up to 10 pooled
   connections per process. Watch that number if you scale replicas.
