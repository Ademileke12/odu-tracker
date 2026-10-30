# ODU Tracker website

The ODU Tracker landing page — cream dot-grid UI with the unique gauge
logo, a CSS-built dashboard mock, live latest-release data pulled from
GitHub, and direct download buttons for every installer this repo's CI
publishes.

```
site/
├── index.html          # the page (hero + mock + features + downloads + how-to)
├── style.css           # cream/dot-grid styling + dark mode
├── script.js           # theme toggle, release fetch, reveal, stats beacon, konami egg
├── vercel.json         # noindex + no-store headers for /admin
├── admin/index.html    # password-gated stats dashboard
└── api/                # Vercel serverless functions (Node, zero deps)
    ├── _lib.js         #   Upstash REST client, PBKDF2 auth, signed session cookies
    ├── track.js        #   POST — public beacon: page views + download clicks
    ├── login.js        #   POST — password check (rate-limited per IP)
    ├── logout.js       #   POST — clears the session cookie
    └── stats.js        #   GET  — auth-gated: counters + real GitHub download counts
```

## Admin stats page (`/admin`)

Password-protected dashboard showing **all-time downloads per file** (real
counts from the GitHub API — retroactive to the first release), plus
**page views** and **download clicks** tracked from the landing page
beacons (counts start when this ships; nothing existed before it).
It lives at `<your-site>/admin` and is excluded from search engines.

### Security model

- The plaintext admin password exists **only in your password manager** —
  code and Vercel hold just a PBKDF2-SHA256 hash (100k iterations).
- Login is rate-limited per IP (8 attempts / 15 min → HTTP 429).
- Sessions are HMAC-signed, expiring (12 h) tokens in an `HttpOnly;
  Secure; SameSite=Strict` cookie — no localStorage, no XSS-readable token.
- Every secret comparison is constant-time; errors never reveal whether
  the password was close.
- The stats endpoint returns **nothing** without a valid session.
- Track endpoint stores counters only — no cookies, no raw IPs (visitor
  dedupe uses a salted hash).

### One-time setup (5 min, free)

1. **Upstash KV** — [console.upstash.com](https://console.upstash.com) →
   *Create Database* (free plan) → copy the **REST** URL + token.
2. **Vercel env vars** — project → *Settings → Environment Variables*, add
   all four from `.env.example` with your real values:
   `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`,
   `ADMIN_PASSWORD_HASH`, `SESSION_SECRET`.
3. **Deploy** (or redeploy) — the `api/` folder is picked up
   automatically; no framework preset needed.
4. Visit `https://<your-site>/admin`, log in with the generated password,
   and confirm the KPIs render. Done.

Without the env vars the site still works fully — `/api/track` fails
silently and `/admin` answers `503 backend not configured`.

## Deploy to Vercel

1. Push this repo to **https://github.com/Ademileke12/odu-tracker.git**.
2. Go to [vercel.com](https://vercel.com) → **Add New… → Project** → import
   `Ademileke12/odu-tracker`.
3. In **Root Directory** choose `site` (Framework Preset: *Other*).
4. Deploy. Done — downloads on the page link straight to GitHub Releases.

## How downloads work

All platform buttons point at stable "latest" URLs — no site updates
needed on a new release:

```
https://github.com/Ademileke12/odu-tracker/releases/latest/download/ODU-Tracker-Setup.exe
https://github.com/Ademileke12/odu-tracker/releases/latest/download/ODU-Tracker-Portable.exe
https://github.com/Ademileke12/odu-tracker/releases/latest/download/odu-tracker-amd64.deb
https://github.com/Ademileke12/odu-tracker/releases/latest/download/ODU-Tracker.apk
```

The **Android APK** button is disabled with a "COMING SOON" badge while the
app is being redesigned for stability — remove the `dl-card-soon` classes
(and restore the old `dl-link` anchor) when the rebuilt APK ships.

Those assets are produced automatically by `.github/workflows/build.yml`
(Windows NSIS + portable, Linux .deb, Android APK) on every push, and
attached to a GitHub Release on every version tag. The page also fetches
`api.github.com/.../releases/latest` to show the newest version number and
file sizes — with a graceful fallback to the Releases page when a release
doesn't exist yet.
