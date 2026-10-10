#!/usr/bin/env python3
"""inspiration — local upload server (Phase 4).

Serves the static site AND provides the upload tool.

  GET  /            → homepage
  GET  /upload      → upload page (tools/upload.html, gitignored)
  GET  /manage      → manage posts page (tools/manage.html, gitignored)
  GET  /api/posts   → list all posts as JSON (+ undo availability)
  POST /api/upload  → save image + post locally (assets/, js/data.js)
  POST /api/edit    → update a post (title/description/related/slug)
  POST /api/delete  → remove a post (stashes it for undo)
  POST /api/undo    → restore the most recently deleted post
  POST /api/push    → git add/commit/push; clears undo history

Local-only: binds 127.0.0.1, never deployed. Run with:

    python3 tools/upload_server.py [port]     # default 8080

Python stdlib only — no dependencies.
"""

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(ROOT, "assets")
DATA_JS = os.path.join(ROOT, "js", "data.js")
POSTS_JSON = os.path.join(ROOT, "js", "posts.json")
UPLOAD_PAGE = os.path.join(ROOT, "tools", "upload.html")

ALLOWED_IMAGE_TYPES = {"png": ".png", "jpeg": ".jpg", "webp": ".webp", "gif": ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB decoded

# Canonical field order for a post record. This is what makes read -> write byte-stable,
# so git diffs only ever show real content changes.
POST_FIELDS = ("slug", "title", "description", "cover", "gallery", "related")


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


# ---------- js/data.js reading ----------

class ParseError(Exception):
    """js/data.js could not be understood. Callers must not write anything."""


class SlugTaken(ParseError):
    """An edit tried to rename a post to a slug another post already uses."""


# ---------- js/posts.json store ----------

def canonical_post(post):
    """Return `post` with the known fields in canonical order; unknown keys kept, last."""
    out = {k: post[k] for k in POST_FIELDS if k in post}
    for k in post:
        if k not in out:
            out[k] = post[k]
    return out


def read_posts(path=None):
    """Read js/posts.json. Raises ParseError on anything unexpected.

    Callers must treat ParseError as "do not write anything".
    """
    path = path or POSTS_JSON
    name = os.path.basename(path)
    if not os.path.exists(path):
        raise ParseError(f"{name} not found")
    try:
        with open(path, encoding="utf-8") as f:
            posts = json.load(f)
    except ValueError as e:
        raise ParseError(f"{name} is not valid JSON: {e}")
    if not isinstance(posts, list):
        raise ParseError(f"{name} must contain a JSON array")
    for i, post in enumerate(posts):
        if not isinstance(post, dict) or not post.get("slug"):
            raise ParseError(f"{name}: entry {i} is not a post object with a slug")
    return [canonical_post(p) for p in posts]


def write_posts(posts, path=None):
    """Write posts to the JSON store atomically. Returns (ok, detail).

    Validation is a round-trip through json.load on the temp file: if what we wrote does
    not read back as the same records, the temp file is removed and the real file is never
    touched. json.dumps cannot emit malformed JSON, so this replaces `node --check`.
    """
    path = path or POSTS_JSON
    records = [canonical_post(p) for p in posts]
    tmp = path + ".tmp"
    try:
        payload = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        with open(tmp, encoding="utf-8") as f:
            if json.load(f) != records:
                raise ValueError("round-trip mismatch")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, str(e)
    return True, "ok"


POSTS_ARRAY_RE = re.compile(r"const\s+POSTS\s*=\s*\[")
STR_RE = r'"((?:[^"\\]|\\.)*)"'
TRASH_DIR = os.path.join(ROOT, ".dev", "trash")


def _skip_string(content, i):
    """`i` points at a quote or backtick. Return the index just past its close."""
    quote = content[i]
    i += 1
    while i < len(content):
        c = content[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    raise ParseError("unterminated string or template literal")


def array_bounds(content):
    """Return (body_start, body_end): indices inside the `POSTS = [ ... ]` brackets."""
    m = POSTS_ARRAY_RE.search(content)
    if not m:
        raise ParseError("could not find `const POSTS = [`")
    i = m.end()  # just after the opening '['
    depth = 0
    while i < len(content):
        c = content[i]
        if c in "\"'`":
            i = _skip_string(content, i)
            continue
        if c in "[{(":
            depth += 1
        elif c in "]})":
            if c == "]" and depth == 0:
                return m.end(), i
            depth -= 1
        i += 1
    raise ParseError("could not find the closing `]` of the POSTS array")


def post_block_spans(content):
    """Return [(start, end), ...] — one span per top-level `{...}` object.

    String literals are skipped, so braces and newlines inside descriptions are safe.
    """
    body_start, body_end = array_bounds(content)
    spans = []
    i = body_start
    depth = 0
    opened_at = None
    while i < body_end:
        c = content[i]
        if c in "\"'`":
            i = _skip_string(content, i)
            continue
        if c == "{":
            if depth == 0:
                # Include the leading indentation so rebuilding the array keeps
                # the file's formatting byte-identical.
                j = i
                while j > body_start and content[j - 1] in " \t":
                    j -= 1
                opened_at = j
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and opened_at is not None:
                spans.append((opened_at, i + 1))
                opened_at = None
        i += 1
    if depth != 0:
        raise ParseError("unbalanced braces in POSTS array")
    return spans


def _unescape_str(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _unescape_template(s):
    return s.replace("\\`", "`").replace("\\${", "${").replace("\\\\", "\\")


def parse_block(text):
    """Extract post fields from one `{ ... }` block."""

    def field(name, pattern=STR_RE):
        # anchored at line start so a description mentioning e.g. "cover:" cannot match
        m = re.search(rf"^[ \t]*{name}:\s*{pattern}", text, re.S | re.M)
        if not m:
            raise ParseError(f"missing or malformed field `{name}` in block: {text[:70]!r}...")
        return m.group(1)

    return {
        "slug": _unescape_str(field("slug")),
        "title": _unescape_str(field("title")),
        "description": _unescape_template(field("description", r"`((?:\\.|[^`\\])*)`")),
        "cover": _unescape_str(field("cover")),
        "gallery": [_unescape_str(x) for x in re.findall(STR_RE, field("gallery", r"\[([^\]]*)\]"))],
        "related": [_unescape_str(x) for x in re.findall(STR_RE, field("related", r"\[([^\]]*)\]"))],
    }


def parse_posts(content):
    """Parse every post in js/data.js. Raises ParseError on anything unexpected."""
    return [parse_block(content[s:e]) for s, e in post_block_spans(content)]


def read_data_js(path=None):
    path = path or DATA_JS
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise ParseError(f"{path} not found")


# ---------- js/data.js writing ----------

def render_post_block(post):
    """Render one post as a canonical 4-space-indented block (no trailing comma)."""
    gallery_inner = ", ".join(f'"{js_str(g)}"' for g in post["gallery"])
    related_inner = ", ".join(f'"{js_str(r)}"' for r in post["related"])
    return (
        "    {\n"
        f'        slug: "{js_str(post["slug"])}",\n'
        f'        title: "{js_str(post["title"])}",\n'
        f"        description: `{js_template(post['description'])}`,\n"
        f'        cover: "{js_str(post["cover"])}",\n'
        f"        gallery: [{gallery_inner}],\n"
        f"        related: [{related_inner}]\n"
        "    }"
    )


def block_texts(content):
    """Current text of each post block, verbatim (formatting preserved)."""
    return [content[s:e] for s, e in post_block_spans(content)]


def replace_array_body(content, blocks):
    """Rebuild js/data.js with `blocks` as the POSTS entries, joined by commas."""
    body_start, body_end = array_bounds(content)
    body = "\n" if not blocks else "\n" + ",\n".join(blocks) + "\n"
    return content[:body_start] + body + content[body_end:]


def post_index(content, slug):
    """Index of the post with this slug, or -1."""
    for i, (s, e) in enumerate(post_block_spans(content)):
        m = re.search(r"^[ \t]*slug:\s*" + STR_RE, content[s:e], re.M)
        if m and _unescape_str(m.group(1)) == slug:
            return i
    return -1


def insert_block_at(content, index, block):
    """Insert a previously removed block text at `index` (clamped)."""
    blocks = block_texts(content)
    idx = max(0, min(index, len(blocks)))
    blocks.insert(idx, block)
    return replace_array_body(content, blocks)


def js_check_file(path):
    """Return (ok, detail) for whether `path` parses as JavaScript."""
    if shutil.which("node"):
        proc = subprocess.run(
            ["node", "--check", path],
            capture_output=True, text=True,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()
    text = open(path, encoding="utf-8").read()
    try:
        array_bounds(text)
    except ParseError as e:
        return False, str(e)
    for open_c, close_c in (("{", "}"), ("[", "]"), ("(", ")")):
        if text.count(open_c) != text.count(close_c):
            return False, f"unbalanced {open_c}{close_c}"
    return True, "rough balance check passed"


def write_data_js(new_content, path=None):
    """Validate `new_content` as JS, then atomically replace `path`.

    Validation happens on a temp file BEFORE the real file is touched, so a bad
    edit can never break the live site. Returns (ok, detail).
    """
    path = path or DATA_JS
    # NOTE: the temp file must keep a .js extension — `node --check` refuses
    # other extensions (ERR_UNKNOWN_FILE_EXTENSION).
    tmp = path + ".tmp.js"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_content)
        ok, detail = js_check_file(tmp)
        if not ok:
            os.remove(tmp)
            return False, detail
        os.replace(tmp, path)
        return True, ""
    except OSError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, str(e)


def js_check_syntax():
    """Kept for compatibility with existing callers."""
    return js_check_file(DATA_JS)


# ---------- post operations ----------

def append_post(post):
    """Append a post to js/data.js. Returns (ok, detail)."""
    content = read_data_js()
    blocks = block_texts(content) + [render_post_block(post)]
    return write_data_js(replace_array_body(content, blocks))


def normalize_related(related_raw, exclude=None):
    """Accept a list or comma-separated string; keep only known slugs,
    drop self-references and duplicates."""
    if isinstance(related_raw, str):
        items = [s.strip() for s in related_raw.split(",")]
    else:
        items = [str(s).strip() for s in (related_raw or [])]
    known = existing_slugs()
    out = []
    for s in items:
        if not s or s == exclude or s not in known or s in out:
            continue
        out.append(s)
    return out


def apply_edit(content, slug, title, description, related, new_slug=None):
    """Edit a post's fields, optionally renaming its slug and fixing every
    `related` reference to the old slug.

    Returns (new_content, final_slug, refs_updated).
    Raises ParseError if the post is missing; SlugTaken if the new slug is in use.
    """
    posts = parse_posts(content)
    blocks = block_texts(content)

    idx = next((i for i, p in enumerate(posts) if p["slug"] == slug), -1)
    if idx == -1:
        raise ParseError(f"no post with slug '{slug}'")

    final_slug = (new_slug or "").strip() or slug
    if final_slug != slug:
        taken = {p["slug"] for i, p in enumerate(posts) if i != idx}
        if final_slug in taken:
            raise SlugTaken(final_slug)

    blocks[idx] = render_post_block({
        **posts[idx],
        "slug": final_slug,
        "title": title,
        "description": description,
        "related": [r for r in related if r != final_slug],
    })

    refs_updated = 0
    if final_slug != slug:
        for i, p in enumerate(posts):
            if i == idx or slug not in p["related"]:
                continue
            blocks[i] = render_post_block(
                {**p, "related": [final_slug if r == slug else r for r in p["related"]]}
            )
            refs_updated += 1

    return replace_array_body(content, blocks), final_slug, refs_updated


# ---------- delete stash / undo ----------

def stash_delete(slug, title, block, index, abs_paths, trash_root=None, guard_root=None):
    """Move a deleted post's image files into the trash and persist an undo record.

    `abs_paths` are absolute file paths. Files are only moved when they live under
    `guard_root` (default: assets/). Returns the record dict.
    """
    trash_root = trash_root or TRASH_DIR
    guard_root = guard_root or ASSETS_DIR
    dest = os.path.join(trash_root, f"{int(time.time())}-{slug}")
    os.makedirs(dest, exist_ok=True)

    files = []
    for full in sorted(set(abs_paths)):
        full = os.path.abspath(full)
        if not full.startswith(guard_root + os.sep) or not os.path.isfile(full):
            continue
        target = os.path.join(dest, os.path.basename(full))
        shutil.move(full, target)
        files.append({"original": full, "trashed": target})

    record = {
        "slug": slug,
        "title": title,
        "block": block,
        "index": index,
        "stamp": int(time.time()),
        "dir": dest,
        "files": files,
    }
    with open(os.path.join(dest, "record.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    return record


def load_undo_stack(trash_root=None):
    """Rebuild the undo stack from <trash>/*/record.json, oldest first."""
    trash_root = trash_root or TRASH_DIR
    if not os.path.isdir(trash_root):
        return []
    out = []
    for name in sorted(os.listdir(trash_root)):
        p = os.path.join(trash_root, name, "record.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def undo_last_delete(trash_root=None, data_path=None):
    """Restore the most recently deleted post: entry first, then its image files.

    Returns (record, error_message); exactly one is truthy.
    """
    data_path = data_path or DATA_JS
    stack = load_undo_stack(trash_root)
    if not stack:
        return None, "nothing to undo"
    record = stack[-1]

    try:
        content = read_data_js(data_path)
        if post_index(content, record["slug"]) != -1:
            return None, f"a post with slug '{record['slug']}' already exists"
        ok, detail = write_data_js(
            insert_block_at(content, record["index"], record["block"]), data_path
        )
        if not ok:
            return None, f"could not restore: {detail}"
    except ParseError as e:
        return None, str(e)

    # only after data.js is safely written, put the images back
    for f in record.get("files", []):
        src, dst = f["trashed"], f["original"]
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)

    shutil.rmtree(record.get("dir") or "", ignore_errors=True)
    return record, ""


def clear_undo_history(trash_root=None):
    """Drop all undo records and trashed files (called after a successful push)."""
    shutil.rmtree(trash_root or TRASH_DIR, ignore_errors=True)


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
        # Local preview must never serve a stale store after an upload.
        if self.path.split("?")[0].endswith(".json"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- GET --

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/posts":
            self.api_posts()
            return
        if path == "/":
            path = "/index.html"
        elif path == "/upload":
            self.serve_file("/tools/upload.html")
            return
        elif path == "/manage":
            self.serve_file("/tools/manage.html")
            return
        self.serve_file(path)

    # -- POST --

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/upload":
            self.api_upload()
        elif path == "/api/edit":
            self.api_edit()
        elif path == "/api/delete":
            self.api_delete()
        elif path == "/api/undo":
            self.api_undo()
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

        slug = unique_slug(slugify(title))
        related = normalize_related(related_raw, exclude=slug)

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

        ok, detail = append_post(post)
        if not ok:
            os.remove(os.path.join(ASSETS_DIR, fname))  # undo the saved image
            self.send_json(500, {"error": f"could not update js/data.js: {detail}"})
            return

        self.send_json(200, {
            "ok": True,
            "slug": slug,
            "url": f"post.html?slug={slug}",
            "cover": cover,
            "message": f"saved '{title}' — view locally or push to repo",
        })

    # -- /api/posts --

    def api_posts(self):
        try:
            posts = parse_posts(read_data_js())
        except ParseError as e:
            self.send_json(500, {"error": f"could not parse js/data.js: {e}"})
            return
        stack = load_undo_stack()
        self.send_json(200, {
            "posts": posts,
            "undo": {
                "count": len(stack),
                "last_title": stack[-1]["title"] if stack else None,
            },
        })

    # -- /api/edit --

    def api_edit(self):
        data = self.read_json_body()
        if not data:
            self.send_json(400, {"error": "invalid JSON body"})
            return

        slug = str(data.get("slug") or "").strip()
        new_slug_raw = str(data.get("new_slug") or "").strip()
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "")
        related_raw = data.get("related") or []

        if not slug:
            self.send_json(400, {"error": "slug is required"})
            return
        if not title:
            self.send_json(400, {"error": "title is required"})
            return
        if not description.strip():
            self.send_json(400, {"error": "description is required"})
            return

        new_slug = slugify(new_slug_raw) if new_slug_raw else slug
        if new_slug_raw and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", new_slug):
            self.send_json(400, {"error": "slug must contain only a-z, 0-9 and dashes"})
            return

        try:
            content = read_data_js()
            if post_index(content, slug) == -1:
                self.send_json(404, {"error": f"no post with slug '{slug}'"})
                return
            new_content, final_slug, refs = apply_edit(
                content, slug, title, description,
                normalize_related(related_raw, exclude=new_slug),
                new_slug=new_slug,
            )
            ok, detail = write_data_js(new_content)
        except SlugTaken as e:
            self.send_json(400, {"error": f"slug '{e}' is already used by another post"})
            return
        except ParseError as e:
            self.send_json(500, {"error": f"could not parse js/data.js: {e}"})
            return

        if not ok:
            self.send_json(500, {"error": f"edit produced invalid JS — not written: {detail}"})
            return

        msg = f"updated '{title}'"
        if final_slug != slug:
            msg += f" and renamed the slug to '{final_slug}'"
            if refs:
                msg += f" ({refs} related reference(s) updated)"
        msg += " — push to repo to publish"

        self.send_json(200, {
            "ok": True,
            "slug": final_slug,
            "old_slug": slug,
            "refs_updated": refs,
            "url": f"post.html?slug={final_slug}",
            "message": msg,
        })

    # -- /api/delete --

    def api_delete(self):
        data = self.read_json_body() or {}
        slug = str(data.get("slug") or "").strip()
        delete_images = bool(data.get("delete_images", True))

        if not slug:
            self.send_json(400, {"error": "slug is required"})
            return

        try:
            content = read_data_js()
            idx = post_index(content, slug)
            if idx == -1:
                self.send_json(404, {"error": f"no post with slug '{slug}'"})
                return

            posts = parse_posts(content)
            target = posts[idx]
            blocks = block_texts(content)
            removed_block = blocks[idx]
            del blocks[idx]
            new_content = replace_array_body(content, blocks)

            if len(parse_posts(new_content)) != len(posts) - 1:
                self.send_json(500, {"error": "delete would not remove exactly one post — aborted"})
                return

            # which of this post's files are no longer referenced by anything?
            rel_paths = []
            if delete_images:
                candidates = {target["cover"], *target["gallery"]}
                still_used = set()
                for p in parse_posts(new_content):
                    still_used.update({p["cover"], *p["gallery"]})
                rel_paths = sorted(candidates - still_used)

            ok, detail = write_data_js(new_content)
        except ParseError as e:
            self.send_json(500, {"error": f"could not parse js/data.js: {e}"})
            return

        if not ok:
            self.send_json(500, {"error": f"delete produced invalid JS — not written: {detail}"})
            return

        record = stash_delete(
            slug, target["title"], removed_block, idx,
            [os.path.join(ROOT, rel) for rel in rel_paths],
        )
        trashed = [f["original"].replace(ROOT + os.sep, "") for f in record["files"]]

        msg = f"deleted '{target['title']}'"
        if trashed:
            msg += f" and moved {len(trashed)} image file(s) to the trash"
        msg += " — push to publish, or restore until then"

        self.send_json(200, {
            "ok": True,
            "slug": slug,
            "trashed": trashed,
            "message": msg,
        })

    # -- /api/undo --

    def api_undo(self):
        record, err = undo_last_delete()
        if err or record is None:
            self.send_json(400, {"error": err or "nothing to undo"})
            return
        self.send_json(200, {
            "ok": True,
            "slug": record["slug"],
            "title": record["title"],
            "restored_files": [f["original"].replace(ROOT + os.sep, "")
                               for f in record.get("files", [])],
            "message": f"restored '{record['title']}'",
        })

    # -- /api/push --

    def api_push(self):
        data = self.read_json_body() or {}
        message = str(data.get("message") or "add: new post").strip()

        # 1. Show what's pending (only the paths this tool owns)
        code, status = run_git(["status", "--short", "--", "js/data.js", "assets/"])
        if code != 0:
            self.send_json(500, {"error": "git status failed", "output": status})
            return

        # 2. Stage data.js and assets — -A also stages deletions from /api/delete
        code, add_out = run_git(["add", "-A", "js/data.js", "assets/"])
        if code != 0:
            self.send_json(500, {"error": "git add failed", "output": add_out})
            return

        # 3. Commit only when THOSE paths actually staged something.
        #    - `git diff --cached --quiet` exits 1 when there are staged changes
        #    - scoped to our paths so unrelated staged files are never swept in
        #    - a retry after a failed push finds nothing staged and skips the commit
        code, _ = run_git(["diff", "--cached", "--quiet", "--", "js/data.js", "assets/"])
        commit_out = ""
        if code == 1:
            code, commit_out = run_git(["commit", "-m", message])
            if code != 0:
                self.send_json(500, {"error": "git commit failed", "output": commit_out})
                return

        # 4. Push — always, so a retry can publish a commit that failed to push earlier.
        code, push_out = run_git(["push"])
        if code != 0:
            self.send_json(500, {
                "error": "git push failed — check auth (SSH key / credential helper)",
                "output": "\n---\n".join(p for p in (commit_out, push_out) if p),
            })
            return

        # 5. Everything is published — pending deletions are now permanent.
        clear_undo_history()

        parts = []
        if status.strip():
            parts.append(f"pending changes:\n{status}")
        if commit_out:
            parts.append(commit_out)
        parts.append(push_out)

        self.send_json(200, {
            "ok": True,
            "pushed": True,
            "output": "\n---\n".join(parts),
            "message": "pushed — deletions are now permanent; GitHub Actions will rebuild & deploy (~1-2 min)",
        })


# ---------- main ----------

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"inspiration local server → http://localhost:{port}")
    print(f"  site:   http://localhost:{port}/")
    print(f"  upload: http://localhost:{port}/upload")
    print(f"  manage: http://localhost:{port}/manage")
    pending = len(load_undo_stack())
    if pending:
        print(f"  undo:   {pending} deleted post(s) can still be restored")
    print("  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
