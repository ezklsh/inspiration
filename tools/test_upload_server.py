#!/usr/bin/env python3
"""Unit tests for the local tool server helpers.

Run:  cd ~/Documents/Projects/Websites/inspiration
      python3 -m unittest discover -s tools -p "test_*.py" -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload_server as srv  # noqa: E402

HAS_NODE = shutil.which("node") is not None
DATA_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "js", "data.js")


def fixture():
    """Two posts with nasty content: real newlines, escaped backtick,
    escaped ${, double quotes in a title, and a full-width unicode paren."""
    return (
        "/* comment */\n"
        "const POSTS = [\n"
        "    {\n"
        '        slug: "alpha",\n'
        '        title: "Alpha One",\n'
        "        description: `line one\nline two \\`tick\\` and \\${tpl} end`,\n"
        '        cover: "assets/a.png",\n'
        '        gallery: ["assets/a.png", "assets/a2.png"],\n'
        '        related: ["beta"]\n'
        "    },\n"
        "    {\n"
        '        slug: "beta",\n'
        '        title: "Beta \\"quoted\\"",\n'
        "        description: `plain`,\n"
        '        cover: "assets/b.png",\n'
        '        gallery: ["assets/b.png"],\n'
        "        related: []\n"
        "    }\n"
        "];\n"
    )


def node_ok(text):
    """Write text to a temp file and run node --check on it."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        return proc.returncode == 0, (proc.stderr or "").strip()
    finally:
        os.remove(path)


class TestParse(unittest.TestCase):
    def test_parse_posts_basic(self):
        posts = srv.parse_posts(fixture())
        self.assertEqual(len(posts), 2)
        a, b = posts
        self.assertEqual(a["slug"], "alpha")
        self.assertEqual(a["title"], "Alpha One")
        self.assertEqual(a["cover"], "assets/a.png")
        self.assertEqual(a["gallery"], ["assets/a.png", "assets/a2.png"])
        self.assertEqual(a["related"], ["beta"])
        self.assertIn("line one\nline two", a["description"])   # newline preserved
        self.assertIn("`tick`", a["description"])               # escaped backtick unescaped
        self.assertIn("${tpl}", a["description"])               # escaped ${ unescaped
        self.assertEqual(b["title"], 'Beta "quoted"')           # escaped quotes unescaped
        self.assertEqual(b["related"], [])

    def test_parse_real_data_js(self):
        with open(DATA_JS, encoding="utf-8") as f:
            posts = srv.parse_posts(f.read())
        self.assertGreaterEqual(len(posts), 3)
        for p in posts:
            self.assertTrue(p["slug"])
            self.assertTrue(p["title"])
            self.assertTrue(p["cover"].startswith("assets/"))
            self.assertIsInstance(p["gallery"], list)
            self.assertIsInstance(p["related"], list)
        self.assertIn("the-odyssey", [p["slug"] for p in posts])

    def test_real_data_description_keeps_quotes(self):
        with open(DATA_JS, encoding="utf-8") as f:
            posts = {p["slug"]: p for p in srv.parse_posts(f.read())}
        ody = posts["the-odyssey"]
        self.assertIn('"All the things you wanted', ody["description"])
        self.assertIn("don't really want to go home", ody["description"])


class TestMutations(unittest.TestCase):
    def test_block_texts_and_index(self):
        content = fixture()
        self.assertEqual(len(srv.block_texts(content)), 2)
        self.assertEqual(srv.post_index(content, "alpha"), 0)
        self.assertEqual(srv.post_index(content, "beta"), 1)
        self.assertEqual(srv.post_index(content, "nope"), -1)

    def test_remove_each_post_stays_valid(self):
        for slug in ("alpha", "beta"):
            content = fixture()
            idx = srv.post_index(content, slug)
            blocks = srv.block_texts(content)
            del blocks[idx]
            new = srv.replace_array_body(content, blocks)
            posts = srv.parse_posts(new)
            self.assertNotIn(slug, [p["slug"] for p in posts])
            self.assertEqual(len(posts), 1)
            if HAS_NODE:
                ok, detail = node_ok(new)
                self.assertTrue(ok, detail)

    def test_remove_all_posts_leaves_empty_array(self):
        content = fixture()
        new = srv.replace_array_body(content, [])
        self.assertEqual(srv.parse_posts(new), [])
        if HAS_NODE:
            ok, detail = node_ok(new)
            self.assertTrue(ok, detail)

    def test_insert_appends_and_stays_valid(self):
        content = fixture()
        new_post = {"slug": "gamma", "title": "Gamma", "description": "third",
                    "cover": "assets/g.png", "gallery": ["assets/g.png"], "related": ["alpha"]}
        blocks = srv.block_texts(content) + [srv.render_post_block(new_post)]
        new = srv.replace_array_body(content, blocks)
        self.assertEqual([p["slug"] for p in srv.parse_posts(new)], ["alpha", "beta", "gamma"])
        if HAS_NODE:
            ok, detail = node_ok(new)
            self.assertTrue(ok, detail)

    def test_insert_block_at_restores_original_position(self):
        content = fixture()
        blocks = srv.block_texts(content)
        removed = blocks.pop(0)
        without = srv.replace_array_body(content, blocks)
        restored = srv.insert_block_at(without, 0, removed)
        self.assertEqual(restored, content)          # byte-identical round trip
        restored_end = srv.insert_block_at(without, 99, removed)
        self.assertEqual([p["slug"] for p in srv.parse_posts(restored_end)], ["beta", "alpha"])

    def test_replace_keeps_other_blocks_byte_identical(self):
        content = fixture()
        blocks = srv.block_texts(content)
        alpha_before = blocks[0]
        idx = srv.post_index(content, "beta")
        target = srv.parse_posts(content)[idx]
        blocks[idx] = srv.render_post_block({**target, "title": "Beta Renamed",
                                            "description": "new\ntext"})
        new = srv.replace_array_body(content, blocks)
        posts = {p["slug"]: p for p in srv.parse_posts(new)}
        self.assertEqual(srv.block_texts(new)[0], alpha_before)        # untouched
        self.assertEqual(posts["beta"]["title"], "Beta Renamed")
        self.assertEqual(posts["beta"]["description"], "new\ntext")
        self.assertEqual(posts["beta"]["cover"], "assets/b.png")       # preserved
        self.assertEqual(posts["beta"]["gallery"], ["assets/b.png"])   # preserved
        if HAS_NODE:
            ok, detail = node_ok(new)
            self.assertTrue(ok, detail)

    def test_render_round_trips_escapes(self):
        post = {"slug": "s", "title": 'Q "quoted"', "description": "a\nb `tick` ${x} \\ end",
                "cover": "assets/x.png", "gallery": ["assets/x.png"], "related": []}
        content = "const POSTS = [\n" + srv.render_post_block(post) + "\n];\n"
        self.assertEqual(srv.parse_posts(content), [post])
        if HAS_NODE:
            ok, detail = node_ok(content)
            self.assertTrue(ok, detail)


class TestNormalizeRelated(unittest.TestCase):
    def test_filters_unknown_drops_self_and_dedupes(self):
        srv.existing_slugs = lambda: {"alpha", "beta"}
        self.assertEqual(srv.normalize_related("beta, nope, beta", exclude="alpha"), ["beta"])
        self.assertEqual(srv.normalize_related(["beta", "beta"], exclude="beta"), [])
        self.assertEqual(srv.normalize_related("", exclude="alpha"), [])


class TestApplyEdit(unittest.TestCase):
    def test_edit_fields_only(self):
        new, final_slug, refs = srv.apply_edit(fixture(), "alpha", "New Title", "new desc", ["beta"])
        self.assertEqual(final_slug, "alpha")
        self.assertEqual(refs, 0)
        posts = {p["slug"]: p for p in srv.parse_posts(new)}
        self.assertEqual(posts["alpha"]["title"], "New Title")
        self.assertEqual(posts["alpha"]["cover"], "assets/a.png")        # preserved
        self.assertEqual(posts["alpha"]["gallery"], ["assets/a.png", "assets/a2.png"])
        if HAS_NODE:
            ok, detail = node_ok(new)
            self.assertTrue(ok, detail)

    def test_rename_updates_related_references(self):
        # alpha's related is ["beta"], so renaming beta must fix alpha
        new, final_slug, refs = srv.apply_edit(fixture(), "beta", "Beta2", "d", [], new_slug="beta-two")
        self.assertEqual(final_slug, "beta-two")
        posts = {p["slug"]: p for p in srv.parse_posts(new)}
        self.assertNotIn("beta", posts)
        self.assertEqual(posts["beta-two"]["title"], "Beta2")
        self.assertEqual(posts["alpha"]["related"], ["beta-two"])        # reference fixed
        self.assertEqual(refs, 1)
        if HAS_NODE:
            ok, detail = node_ok(new)
            self.assertTrue(ok, detail)

    def test_rename_to_existing_slug_is_rejected(self):
        with self.assertRaises(srv.SlugTaken):
            srv.apply_edit(fixture(), "alpha", "t", "d", [], new_slug="beta")

    def test_missing_post_raises(self):
        with self.assertRaises(srv.ParseError):
            srv.apply_edit(fixture(), "nope", "t", "d", [])


class TestStashUndo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.trash = os.path.join(self.tmp, "trash")
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stash_moves_files_and_writes_record(self):
        img = os.path.join(self.assets, "x.png")
        with open(img, "w") as f:
            f.write("data")
        rec = srv.stash_delete("gamma", "Gamma", "    { }", 1, [img],
                               trash_root=self.trash, guard_root=self.assets)
        self.assertFalse(os.path.exists(img))                      # moved out of assets
        self.assertTrue(os.path.isfile(rec["files"][0]["trashed"]))
        stack = srv.load_undo_stack(self.trash)
        self.assertEqual(len(stack), 1)
        self.assertEqual(stack[0]["slug"], "gamma")

    def test_stash_ignores_files_outside_guard_root(self):
        outside = os.path.join(self.tmp, "outside.png")
        with open(outside, "w") as f:
            f.write("data")
        rec = srv.stash_delete("gamma", "Gamma", "    { }", 0, [outside],
                               trash_root=self.trash, guard_root=self.assets)
        self.assertEqual(rec["files"], [])
        self.assertTrue(os.path.isfile(outside))

    def test_full_delete_then_undo_restores_data_js_and_image(self):
        # work on a temp copy of the real data.js
        data = os.path.join(self.tmp, "data.js")
        shutil.copy(DATA_JS, data)
        original = open(data, encoding="utf-8").read()

        img = os.path.join(self.assets, "the-odyssey.webp")
        with open(img, "w") as f:
            f.write("img")

        content = srv.read_data_js(data)
        idx = srv.post_index(content, "the-odyssey")
        blocks = srv.block_texts(content)
        removed_block = blocks[idx]
        del blocks[idx]
        ok, detail = srv.write_data_js(srv.replace_array_body(content, blocks), data)
        self.assertTrue(ok, detail)
        self.assertNotIn("the-odyssey",
                         [p["slug"] for p in srv.parse_posts(srv.read_data_js(data))])

        srv.stash_delete("the-odyssey", "The Odyssey", removed_block, idx, [img],
                         trash_root=self.trash, guard_root=self.assets)

        rec, err = srv.undo_last_delete(trash_root=self.trash, data_path=data)
        self.assertEqual(err, "")
        self.assertEqual(rec["slug"], "the-odyssey")
        self.assertEqual(open(data, encoding="utf-8").read(), original)   # byte-identical
        self.assertTrue(os.path.isfile(img))                              # image back
        self.assertEqual(srv.load_undo_stack(self.trash), [])

    def test_undo_with_empty_stack_errors(self):
        rec, err = srv.undo_last_delete(trash_root=self.trash)
        self.assertIsNone(rec)
        self.assertEqual(err, "nothing to undo")


if __name__ == "__main__":
    unittest.main()
