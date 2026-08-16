#!/usr/bin/env python3
"""inspiration — local upload server (Phase 4).

Serves the static site AND provides the upload tool.

  GET  /            → homepage
  GET  /upload      → upload page (tools/upload.html, gitignored)
  POST /api/upload  → save image + post locally (assets/, js/data.js)
  POST /api/push    → git add/commit/push the uploads

Local-only: binds 127.0.0.1, never deployed. Run with:

    python3 tools/upload_server.py [port]     # default 8080

Python stdlib only — no dependencies.
"""

import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(ROOT, "assets")
DATA_JS = os.path.join(ROOT, "js", "data.js")
UPLOAD_PAGE = os.path.join(ROOT, "tools", "upload.html")

ALLOWED_IMAGE_TYPES = {"png": ".png", "jpeg": ".jpg", "webp": ".webp", "gif": ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB decoded


# ---------- helpers ----------

def slugify(title):
    """'Evangelion Unit-01!' → 'evangelion-unit-01'"""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "post"


def existing_slugs():
    """Extract slugs already present in js/data.js."""
    try:
        with open(DATA_JS, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return set()
    return set(re.findall(r'slug:\s*"([^"]+)"', content))


def unique_slug(base):
    """Append -2, -3… until the slug is unused."""
    slugs = existing_slugs()
    if base not in slugs:
        return base
    i = 2
    while f"{base}-{i}" in slugs:
        i += 1
    return f"{base}-{i}"


def js_str(s):
    """Escape a string for a double-quoted JS string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def js_template(s):
    """Escape text for a JS backtick template literal."""
    return s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def js_check_syntax():
    """Validate js/data.js parses. Uses node if available; else a rough
    brace/paren balance check. Returns (ok, detail)."""
    import shutil
    if shutil.which("node"):
        proc = subprocess.run(
            ["node", "--check", DATA_JS],
            capture_output=True, text=True,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()
    # Fallback: balance check on the raw text
    text = open(DATA_JS, encoding="utf-8").read()
    for open_c, close_c in (("{", "}"), ("[", "]"), ("(", ")")):
        if text.count(open_c) != text.count(close_c):
            return False, f"unbalanced {open_c}{close_c}"
    return True, "rough balance check passed"


def append_post(post):
    """Insert a post object into js/data.js before the closing `];`.

    The previous entry's closing `}` needs a comma after it, or the whole
    file fails to parse (array elements must be comma-separated).
    """
    with open(DATA_JS, encoding="utf-8") as f:
        content = f.read()

    idx = content.rfind("];")
    if idx == -1:
        return False

    related_inner = ", ".join(f'"{r}"' for r in post["related"])

    entry = f"""
    {{
        slug: "{js_str(post['slug'])}",
        title: "{js_str(post['title'])}",
        description: `{js_template(post['description'])}`,
        cover: "{post['cover']}",
        gallery: [{', '.join(f'"{g}"' for g in post['gallery'])}],
        related: [{related_inner}]
    }}
"""

    # If the previous entry doesn't already end with a comma, add one.
    # Strip trailing whitespace first so the comma sits right after the brace.
    base = content[:idx].rstrip()
    prefix = "," if not base.endswith(",") else ""
    new_content = base + prefix + "\n" + entry.lstrip("\n") + "];\n"
    with open(DATA_JS, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def run_git(args):
    """Run a git command in the repo, return (code, output)."""
    proc = subprocess.run(
        ["git"] + args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("[upload] %s\n" % (format % args))

    # -- helpers --

    def send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > 10 * 1024 * 1024:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def serve_file(self, path):
        """Serve a file from the repo root; 404 outside it."""
        if not path or path.startswith("/api/"):
            self.send_json(404, {"error": "not found"})
            return
        full = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            self.send_json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- GET --

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            path = "/index.html"
        elif path == "/upload":
            self.serve_file("/tools/upload.html")
            return
        self.serve_file(path)

    # -- POST --

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/upload":
            self.api_upload()
        elif path == "/api/push":
            self.api_push()
        else:
            self.send_json(404, {"error": "not found"})

    # -- /api/upload --

    def api_upload(self):
        data = self.read_json_body()
        if not data:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        image = str(data.get("image") or "")  # data URL: data:image/png;base64,...
        related_raw = data.get("related") or []

        if not title:
            self.send_json(400, {"error": "title is required"})
            return
        if not description:
            self.send_json(400, {"error": "description is required"})
            return
        if not image:
            self.send_json(400, {"error": "image is required"})
            return

        # Parse data URL: data:image/png;base64,XXXX
        m = re.match(r"^data:image/(png|jpeg|webp|gif);base64,(.+)$", image, re.S)
        if not m:
            self.send_json(400, {"error": "image must be a data URL (png/jpeg/webp/gif)"})
            return
        img_type, b64 = m.group(1), m.group(2)
        ext = ALLOWED_IMAGE_TYPES[img_type]

        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            self.send_json(400, {"error": "invalid base64 image data"})
            return

        if not raw:
            self.send_json(400, {"error": "image is empty"})
            return
        if len(raw) > MAX_IMAGE_BYTES:
            self.send_json(413, {"error": "image too large (max 10MB)"})
            return

        # Related: accept list or comma-separated string; keep only known slugs
        if isinstance(related_raw, str):
            related = [s.strip() for s in related_raw.split(",") if s.strip()]
        else:
            related = [str(s).strip() for s in related_raw if str(s).strip()]
        known = existing_slugs()
        related = [s for s in related if s in known]

        slug = unique_slug(slugify(title))
        fname = f"{slug}-{int(time.time())}{ext}"
        cover = f"assets/{fname}"

        try:
            with open(os.path.join(ASSETS_DIR, fname), "wb") as f:
                f.write(raw)
        except OSError as e:
            self.send_json(500, {"error": f"could not save image: {e}"})
            return

        post = {
            "slug": slug,
            "title": title,
            "description": description,
            "cover": cover,
            "gallery": [cover],
            "related": related,
        }

        if not append_post(post):
            self.send_json(500, {"error": "could not update js/data.js"})
            return

        # Safety net: never leave a broken data.js behind.
        ok, detail = js_check_syntax()
        if not ok:
            # roll back the append so the site keeps working
            with open(DATA_JS, encoding="utf-8") as f:
                content = f.read()
            idx = content.rfind(f'"assets/{fname}"')
            if idx != -1:
                # find the enclosing block start and remove through the comma
                block_start = content.rfind("\n    {", 0, idx)
                block_end = content.find("};", idx) + 2
                new_content = content[:block_start] + content[block_end:]
                with open(DATA_JS, "w", encoding="utf-8") as f:
                    f.write(new_content)
            os.remove(os.path.join(ASSETS_DIR, fname))
            self.send_json(500, {"error": f"append produced invalid JS — rolled back: {detail}"})
            return

        self.send_json(200, {
            "ok": True,
            "slug": slug,
            "url": f"post.html?slug={slug}",
            "cover": cover,
            "message": f"saved '{title}' — view locally or push to repo",
        })

    # -- /api/push --

    def api_push(self):
        data = self.read_json_body() or {}
        message = str(data.get("message") or "add: new post").strip()

        # 1. Show what's pending
        code, status = run_git(["status", "--short"])
        if code != 0:
            self.send_json(500, {"error": "git status failed", "output": status})
            return

        if not status.strip():
            self.send_json(200, {
                "ok": True,
                "pushed": False,
                "output": "nothing to commit — working tree clean",
            })
            return

        # 2. Stage only what uploads produce
        code, add_out = run_git(["add", "js/data.js", "assets/"])
        if code != 0:
            self.send_json(500, {"error": "git add failed", "output": add_out})
            return

        # 3. Commit
        code, commit_out = run_git(["commit", "-m", message])
        if code != 0:
            self.send_json(500, {"error": "git commit failed", "output": commit_out})
            return

        # 4. Push
        code, push_out = run_git(["push"])
        if code != 0:
            self.send_json(500, {
                "error": "git push failed — check auth (SSH key / credential helper)",
                "output": f"{commit_out}\n---\n{push_out}",
            })
            return

        self.send_json(200, {
            "ok": True,
            "pushed": True,
            "output": f"pending changes:\n{status}\n---\n{commit_out}\n---\n{push_out}",
            "message": "pushed — GitHub Actions will rebuild & deploy the site (~1-2 min)",
        })


# ---------- main ----------

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"inspiration local server → http://localhost:{port}")
    print(f"  site:   http://localhost:{port}/")
    print(f"  upload: http://localhost:{port}/upload")
    print("  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
