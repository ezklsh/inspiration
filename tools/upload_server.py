#!/usr/bin/env python3
"""inspiration — local upload server (Phase 4).

Serves the static site AND provides the upload tool.

  GET  /            → homepage
  GET  /upload      → upload page (tools/upload.html, gitignored)
  GET  /manage      → manage posts page (tools/manage.html, gitignored)
  GET  /api/posts   → list all posts as JSON (+ undo availability)
  POST /api/upload  → save image + post locally (assets/, js/posts.json)
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
    """Slugs already present in the store."""
    try:
        return {p["slug"] for p in read_posts()}
    except (ParseError, KeyError):
        return set()


def unique_slug(base):
    """Append -2, -3… until the slug is unused."""
    slugs = existing_slugs()
    if base not in slugs:
        return base
    i = 2
    while f"{base}-{i}" in slugs:
        i += 1
    return f"{base}-{i}"


# ---------- errors ----------

class ParseError(Exception):
    """The post store could not be understood. Callers must not write anything."""


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


TRASH_DIR = os.path.join(ROOT, ".dev", "trash")


# ---------- post operations ----------

def append_post(post):
    """Append a post to the store. Returns (ok, detail)."""
    try:
        posts = read_posts()
    except ParseError as e:
        return False, f"could not read the store: {e}"
    posts.append(canonical_post(post))
    return write_posts(posts)


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


def apply_edit(posts, slug, title, description, related, new_slug=None):
    """Edit a post's fields, optionally renaming its slug and fixing every
    `related` reference to the old slug.

    Returns (new_posts, final_slug, refs_updated) where refs_updated is the list of posts
    whose `related` was repointed.
    Raises ParseError if the post is missing; SlugTaken if the new slug is in use.
    """
    idx = next((i for i, p in enumerate(posts) if p["slug"] == slug), -1)
    if idx == -1:
        raise ParseError(f"no post with slug '{slug}'")

    final_slug = (new_slug or "").strip() or slug
    if final_slug != slug:
        taken = {p["slug"] for i, p in enumerate(posts) if i != idx}
        if final_slug in taken:
            raise SlugTaken(final_slug)

    updated = [dict(p) for p in posts]
    target = updated[idx]
    target["slug"] = final_slug
    target["title"] = title
    target["description"] = description
    target["related"] = [r for r in (related or []) if r != final_slug]

    refs_updated = []
    if final_slug != slug:
        for p in updated:
            if p is target or slug not in (p.get("related") or []):
                continue
            p["related"] = [final_slug if r == slug else r for r in p["related"]]
            refs_updated.append(p["slug"])

    return [canonical_post(p) for p in updated], final_slug, refs_updated


# ---------- delete stash / undo ----------

def stash_delete(slug, title, post, index, abs_paths, trash_root=None, guard_root=None):
    """Move a deleted post's image files into the trash and persist an undo record.

    `abs_paths` are absolute file paths. Files are only moved when they live under
    `guard_root` (default: assets/). `post` is the whole post object, so undo can re-insert
    it without re-parsing anything. Returns the record dict.
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
        "post": canonical_post(post),
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
    """Restore the most recently deleted post: the store first, then its image files.

    Returns (record, error_message); exactly one is truthy.
    """
    data_path = data_path or POSTS_JSON
    stack = load_undo_stack(trash_root)
    if not stack:
        return None, "nothing to undo"
    record = stack[-1]

    if "post" not in record:
        return None, "this undo record predates the JSON store — restore it by hand"

    try:
        posts = read_posts(data_path)
        if any(p.get("slug") == record["slug"] for p in posts):
            return None, f"a post with slug '{record['slug']}' already exists"
        index = min(int(record.get("index", len(posts))), len(posts))
        posts.insert(index, canonical_post(record["post"]))
        ok, detail = write_posts(posts, data_path)
        if not ok:
            return None, f"could not restore: {detail}"
    except ParseError as e:
        return None, str(e)

    # only after the store is safely written, put the images back
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
            self.send_json(500, {"error": f"could not update the post store: {detail}"})
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
            posts = read_posts()
        except ParseError as e:
            self.send_json(500, {"error": f"could not read the post store: {e}"})
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
            posts = read_posts()
        except ParseError as e:
            self.send_json(500, {"error": f"could not read the post store: {e}"})
            return

        if not any(p.get("slug") == slug for p in posts):
            self.send_json(404, {"error": f"no post with slug '{slug}'"})
            return

        try:
            new_posts, final_slug, refs = apply_edit(
                posts, slug, title, description,
                normalize_related(related_raw, exclude=new_slug),
                new_slug=new_slug,
            )
        except SlugTaken as e:
            self.send_json(400, {"error": f"slug '{e}' is already used by another post"})
            return
        except ParseError as e:
            self.send_json(400, {"error": str(e)})
            return

        ok, detail = write_posts(new_posts)
        if not ok:
            self.send_json(500, {"error": f"edit could not be written: {detail}"})
            return

        msg = f"updated '{title}'"
        if final_slug != slug:
            msg += f" and renamed the slug to '{final_slug}'"
            if refs:
                msg += f" ({len(refs)} related reference(s) updated: {', '.join(refs)})"
        msg += " — push to repo to publish"

        self.send_json(200, {
            "ok": True,
            "slug": final_slug,
            "old_slug": slug,
            "refs_updated": len(refs),
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
            posts = read_posts()
            idx = next((i for i, p in enumerate(posts) if p.get("slug") == slug), -1)
            if idx == -1:
                self.send_json(404, {"error": f"no post with slug '{slug}'"})
                return

            target = posts[idx]
            remaining = [p for i, p in enumerate(posts) if i != idx]

            if len(remaining) != len(posts) - 1:
                self.send_json(500, {"error": "delete would not remove exactly one post — aborted"})
                return

            # which of this post's files are no longer referenced by anything?
            rel_paths = []
            if delete_images:
                candidates = {target["cover"], *target["gallery"]}
                still_used = set()
                for p in remaining:
                    still_used.update({p["cover"], *p["gallery"]})
                rel_paths = sorted(candidates - still_used)

            ok, detail = write_posts(remaining)
        except ParseError as e:
            self.send_json(500, {"error": f"could not read the post store: {e}"})
            return

        if not ok:
            self.send_json(500, {"error": f"delete could not be written: {detail}"})
            return

        record = stash_delete(
            slug, target["title"], target, idx,
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
        code, status = run_git(["status", "--short", "--", "js/posts.json", "assets/"])
        if code != 0:
            self.send_json(500, {"error": "git status failed", "output": status})
            return

        # 2. Stage the store and assets — -A also stages deletions from /api/delete
        code, add_out = run_git(["add", "-A", "js/posts.json", "assets/"])
        if code != 0:
            self.send_json(500, {"error": "git add failed", "output": add_out})
            return

        # 3. Commit only when THOSE paths actually staged something.
        #    - `git diff --cached --quiet` exits 1 when there are staged changes
        #    - scoped to our paths so unrelated staged files are never swept in
        #    - a retry after a failed push finds nothing staged and skips the commit
        code, _ = run_git(["diff", "--cached", "--quiet", "--", "js/posts.json", "assets/"])
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
