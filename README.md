# inspiration

a moodboard of creative works (movies, anime, posters, games, architecture etc.) that inspire me.

## Run it locally

The site loads its posts with `fetch()`, so it must be served over HTTP. Opening the files
directly with `file://` shows a "could not load js/posts.json" message — that is by design, and
the message tells you what to run instead.

```
python3 tools/upload_server.py          # → http://localhost:8080
```

| Page | What it does |
| --- | --- |
| <http://localhost:8080/> | the moodboard grid |
| <http://localhost:8080/upload> | add a post (image + title + description) |
| <http://localhost:8080/manage> | edit, rename, delete (+ undo), push to the repo |

**Prerequisites:** Python 3, stdlib only. No build step, no `node`, no dependencies.

## Where the content lives

- `js/posts.json` — the post store: a JSON array of
  `{slug, title, description, cover, gallery, related}`.
- `assets/` — the images. Descriptions are rendered as HTML and newlines are preserved.

Posts are written by the upload/manage pages. If you hand-edit `js/posts.json`, keep it valid
JSON (no trailing commas, no comments) — the local server refuses to write if it cannot read
the store, so a typo can never break the live site.

## Publishing

The **push** button in `/upload` or `/manage` stages `js/posts.json` and `assets/`, commits and
pushes. A push to `main` triggers `.github/workflows/deploy.yml`, which assembles the site and
publishes it to GitHub Pages.

GitHub Pages must be enabled once under **Settings → Pages → Source: GitHub Actions**.

## Tests

```
python3 -m unittest discover -s tools -p "test_*.py"
```
