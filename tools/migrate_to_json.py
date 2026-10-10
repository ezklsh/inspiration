#!/usr/bin/env python3
"""One-off: convert js/data.js (JS literals) into js/posts.json (JSON).

Run ONCE, while the old JS parser still exists:

    python3 tools/migrate_to_json.py

The old parser is the trusted reader here: it parses data.js into records, and this
script refuses to finish if what lands in posts.json does not match those records.

It also rewrites the undo records in .dev/trash/ so their stored JS text block becomes a
post object, because stash_delete/undo_last_delete work on objects after the migration.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload_server as srv  # noqa: E402


def migrate_posts():
    """js/data.js -> js/posts.json, verified field-by-field."""
    posts = srv.parse_posts(srv.read_data_js())
    records = [srv.canonical_post(p) for p in posts]

    ok, detail = srv.write_posts(records, srv.POSTS_JSON)
    if not ok:
        print(f"FAIL: could not write {srv.POSTS_JSON}: {detail}")
        return 1

    with open(srv.POSTS_JSON, encoding="utf-8") as f:
        back = json.load(f)
    if back != records:
        print("FAIL: posts.json does not match the parsed data.js")
        return 1

    for post in records:
        print(f"  ok  {post['slug']:<30} {len(post.get('gallery', []))} image(s)")
    print(f"wrote {srv.POSTS_JSON} ({len(records)} post(s))")
    return 0


def migrate_undo_records():
    """Rewrite each .dev/trash/*/record.json: 'block' (JS text) -> 'post' (dict)."""
    if not os.path.isdir(srv.TRASH_DIR):
        print("no trash: no undo records to convert")
        return 0

    converted = 0
    for name in sorted(os.listdir(srv.TRASH_DIR)):
        rec_path = os.path.join(srv.TRASH_DIR, name, "record.json")
        if not os.path.isfile(rec_path):
            continue

        try:
            with open(rec_path, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  ! {name}: unreadable record ({e}); dropping it")
            os.remove(rec_path)
            continue

        if "block" not in record:
            continue                                    # already converted

        try:
            record["post"] = srv.canonical_post(srv.parse_block(record.pop("block")))
        except Exception as e:                          # noqa: BLE001 - any failure: drop it
            print(f"  ! {name}: could not convert ({e}); dropping this undo record")
            os.remove(rec_path)
            continue

        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        converted += 1
        print(f"  ok  {name}: undo record converted to a post object")

    print(f"converted {converted} undo record(s)")
    return 0


if __name__ == "__main__":
    rc = migrate_posts()
    if rc == 0:
        rc = migrate_undo_records()
    raise SystemExit(rc)
