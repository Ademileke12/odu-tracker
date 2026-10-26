# ODU Tracker website

The ODU Tracker landing page — cream dot-grid UI with the unique gauge
logo, a CSS-built dashboard mock, live latest-release data pulled from
GitHub, and direct download buttons for every installer this repo's CI
publishes.

```
site/
├── index.html      # the page (hero + mock + features + downloads + how-to)
├── style.css       # cream/dot-grid styling + dark mode
└── script.js       # theme toggle, release fetch, reveal, konami boost easter egg
```

## Deploy to Vercel

1. Push this repo to **https://github.com/Ademileke12/odu-tracker.git**.
2. Go to [vercel.com](https://vercel.com) → **Add New… → Project** → import
   `Ademileke12/odu-tracker`.
3. In **Root Directory** choose `site` (Framework Preset: *Other*).
4. Deploy. Done — downloads on the page link straight to GitHub Releases.

## How downloads work

The Windows + Android buttons point at stable "latest" URLs — no site
updates needed on a new release:

```
https://github.com/Ademileke12/odu-tracker/releases/latest/download/ODU-Tracker-Setup.exe
https://github.com/Ademileke12/odu-tracker/releases/latest/download/ODU-Tracker-Portable.exe
https://github.com/Ademileke12/odu-tracker/releases/latest/download/ODU-Tracker.apk
```

The **Linux .deb** button is a direct-download mirror (file.kiwi) until a
GitHub release exists; it's marked `data-external` in `index.html` so the
script's fallback logic never rewrites it.

Those assets are produced automatically by `.github/workflows/build.yml`
(Windows NSIS + portable, Linux .deb, Android APK) on every push, and
attached to a GitHub Release on every version tag. The page also fetches
`api.github.com/.../releases/latest` to show the newest version number and
file sizes — with a graceful fallback to the Releases page when a release
doesn't exist yet.
