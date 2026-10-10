#!/usr/bin/env python3
"""Unit tests for the local tool server helpers.

Run:  cd ~/Documents/Projects/Websites/inspiration
      python3 -m unittest discover -s tools -p "test_*.py" -v

The store is js/posts.json. Tests never assert a post count or a specific slug: the content
is the owner's and changes as they publish, so live-content tests assert STRUCTURE only.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload_server as srv  # noqa: E402


def records():
    """Two post objects in the shape the store holds."""
    return [
        {"slug": "alpha", "title": "Alpha One", "description": "d",
         "cover": "assets/a.png", "gallery": ["assets/a.png", "assets/a2.png"],
         "related": ["beta"]},
        {"slug": "beta", "title": 'Beta "quoted"', "description": "plain",
         "cover": "assets/b.png", "gallery": ["assets/b.png"], "related": []},
    ]


class TestNormalizeRelated(unittest.TestCase):
    def setUp(self):
        self._real = srv.existing_slugs
        srv.existing_slugs = lambda: {"alpha", "beta"}

    def tearDown(self):
        srv.existing_slugs = self._real

    def test_filters_unknown_drops_self_and_dedupes(self):
        self.assertEqual(srv.normalize_related("beta, nope, beta", exclude="alpha"), ["beta"])
        self.assertEqual(srv.normalize_related(["beta", "beta"], exclude="beta"), [])
        self.assertEqual(srv.normalize_related("", exclude="alpha"), [])


class TestApplyEdit(unittest.TestCase):
    def test_edit_fields_only(self):
        new, final_slug, refs = srv.apply_edit(records(), "alpha", "New Title", "new desc",
                                               ["beta"])
        self.assertEqual(final_slug, "alpha")
        self.assertEqual(refs, [])
        got = {p["slug"]: p for p in new}
        self.assertEqual(got["alpha"]["title"], "New Title")
        self.assertEqual(got["alpha"]["description"], "new desc")
        self.assertEqual(got["alpha"]["related"], ["beta"])
        self.assertEqual(got["alpha"]["cover"], "assets/a.png")            # preserved
        self.assertEqual(got["alpha"]["gallery"], ["assets/a.png", "assets/a2.png"])
        self.assertEqual(list(got["alpha"]), list(srv.POST_FIELDS))        # canonical order

    def test_rename_updates_related_references(self):
        # alpha's related is ["beta"], so renaming beta must fix alpha
        new, final_slug, refs = srv.apply_edit(records(), "beta", "Beta2", "d", [],
                                               new_slug="beta-two")
        self.assertEqual(final_slug, "beta-two")
        got = {p["slug"]: p for p in new}
        self.assertNotIn("beta", got)
        self.assertEqual(got["beta-two"]["title"], "Beta2")
        self.assertEqual(got["alpha"]["related"], ["beta-two"])            # reference fixed
        self.assertEqual(refs, ["alpha"])

    def test_self_reference_is_dropped(self):
        new, _, _ = srv.apply_edit(records(), "alpha", "t", "d", ["alpha", "beta"])
        self.assertEqual({p["slug"]: p for p in new}["alpha"]["related"], ["beta"])

    def test_rename_to_existing_slug_is_rejected(self):
        with self.assertRaises(srv.SlugTaken):
            srv.apply_edit(records(), "alpha", "t", "d", [], new_slug="beta")

    def test_missing_post_raises(self):
        with self.assertRaises(srv.ParseError):
            srv.apply_edit(records(), "nope", "t", "d", [])

    def test_input_records_are_not_mutated(self):
        before = records()
        srv.apply_edit(before, "alpha", "New", "d", [])
        self.assertEqual(before, records())


class TestStashUndo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.trash = os.path.join(self.tmp, "trash")
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def image(self, name="x.png"):
        path = os.path.join(self.assets, name)
        with open(path, "w") as f:
            f.write("data")
        return path

    def test_stash_moves_files_and_stores_the_post_object(self):
        img = self.image()
        post = {"slug": "gamma", "title": "Gamma", "description": "d",
                "cover": "assets/x.png", "gallery": ["assets/x.png"], "related": []}
        rec = srv.stash_delete("gamma", "Gamma", post, 1, [img],
                               trash_root=self.trash, guard_root=self.assets)
        self.assertFalse(os.path.exists(img))                       # moved out of assets
        self.assertTrue(os.path.isfile(rec["files"][0]["trashed"]))
        self.assertNotIn("block", rec)                              # no JS text any more
        self.assertEqual(rec["post"]["slug"], "gamma")
        self.assertEqual(list(rec["post"]), list(srv.POST_FIELDS))
        stack = srv.load_undo_stack(self.trash)
        self.assertEqual(len(stack), 1)
        self.assertEqual(stack[0]["slug"], "gamma")

    def test_stash_ignores_files_outside_guard_root(self):
        outside = os.path.join(self.tmp, "outside.png")
        with open(outside, "w") as f:
            f.write("data")
        rec = srv.stash_delete("gamma", "Gamma", {"slug": "gamma"}, 0, [outside],
                               trash_root=self.trash, guard_root=self.assets)
        self.assertEqual(rec["files"], [])
        self.assertTrue(os.path.isfile(outside))

    def test_full_delete_then_undo_restores_the_store_and_image(self):
        store = os.path.join(self.tmp, "posts.json")
        shutil.copy(srv.POSTS_JSON, store)                  # a copy of the owner's content
        original = open(store, encoding="utf-8").read()

        posts = srv.read_posts(store)
        if not posts:
            self.skipTest("the live store is empty")
        first, idx = posts[0], 0

        img = self.image("cover.webp")
        ok, detail = srv.write_posts(posts[1:], store)
        self.assertTrue(ok, detail)
        self.assertNotIn(first["slug"], [p["slug"] for p in srv.read_posts(store)])

        srv.stash_delete(first["slug"], first["title"], first, idx, [img],
                         trash_root=self.trash, guard_root=self.assets)

        rec, err = srv.undo_last_delete(trash_root=self.trash, data_path=store)
        self.assertEqual(err, "")
        self.assertEqual(rec["slug"], first["slug"])
        self.assertEqual(open(store, encoding="utf-8").read(), original)   # byte-identical
        self.assertTrue(os.path.isfile(img))                               # image back
        self.assertEqual(srv.load_undo_stack(self.trash), [])

    def test_undo_refuses_a_pre_migration_record(self):
        os.makedirs(os.path.join(self.trash, "1-old"), exist_ok=True)
        with open(os.path.join(self.trash, "1-old", "record.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"slug": "old", "title": "Old", "block": "    { }",
                       "index": 0, "files": []}, f)
        rec, err = srv.undo_last_delete(trash_root=self.trash)
        self.assertIsNone(rec)
        self.assertIn("predates", err)

    def test_undo_with_empty_stack_errors(self):
        rec, err = srv.undo_last_delete(trash_root=self.trash)
        self.assertIsNone(rec)
        self.assertEqual(err, "nothing to undo")


# ---------- the store itself ----------

class TestCanonicalPost(unittest.TestCase):
    def test_canonical_field_order(self):
        post = {"related": [], "cover": "c.webp", "slug": "s", "title": "t",
                "description": "d", "gallery": ["g.webp"]}
        self.assertEqual(list(srv.canonical_post(post)),
                         ["slug", "title", "description", "cover", "gallery", "related"])

    def test_unknown_keys_are_preserved_last(self):
        out = srv.canonical_post({"slug": "s", "title": "t", "color": "#fff"})
        self.assertEqual(list(out), ["slug", "title", "color"])
        self.assertEqual(out["color"], "#fff")


class TestReadPosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "posts.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return self.path

    def test_reads_a_list(self):
        p = self.write('[{"slug": "a", "title": "A"}]')
        self.assertEqual([q["slug"] for q in srv.read_posts(p)], ["a"])

    def test_missing_file_raises_parse_error(self):
        with self.assertRaises(srv.ParseError):
            srv.read_posts(os.path.join(self.tmp, "nope.json"))

    def test_invalid_json_raises_parse_error(self):
        p = self.write('[{"slug": "a"},]')          # trailing comma is not JSON
        with self.assertRaises(srv.ParseError):
            srv.read_posts(p)

    def test_non_array_raises_parse_error(self):
        p = self.write('{"slug": "a"}')
        with self.assertRaises(srv.ParseError):
            srv.read_posts(p)

    def test_entry_without_slug_raises_parse_error(self):
        p = self.write('[{"title": "no slug"}]')
        with self.assertRaises(srv.ParseError):
            srv.read_posts(p)

    def test_unicode_is_preserved(self):
        p = self.write('[{"slug": "a", "description": "\uff08Shinya Edaki\uff09"}]')
        self.assertEqual(srv.read_posts(p)[0]["description"], "\uff08Shinya Edaki\uff09")


class TestWritePosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "posts.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_writes_pretty_json_with_trailing_newline(self):
        ok, detail = srv.write_posts([{"slug": "a", "title": "A"}], self.path)
        self.assertTrue(ok, detail)
        raw = self.read()
        self.assertTrue(raw.endswith("]\n"))
        self.assertIn('\n  {\n    "slug": "a"', raw)

    def test_round_trip_is_byte_identical(self):
        srv.write_posts([{"slug": "a", "title": "A", "description": "x\ny"}], self.path)
        first = self.read()
        srv.write_posts(srv.read_posts(self.path), self.path)
        self.assertEqual(first, self.read())

    def test_unicode_stays_literal(self):
        srv.write_posts([{"slug": "a", "description": "\uff08Edaki\uff09"}], self.path)
        raw = self.read()
        self.assertIn("\uff08Edaki\uff09", raw)
        self.assertNotIn("\\u", raw)

    def test_unserializable_input_leaves_file_untouched(self):
        srv.write_posts([{"slug": "a"}], self.path)
        before = self.read()
        ok, detail = srv.write_posts([{"slug": "b", "bad": {1, 2}}], self.path)  # a set
        self.assertFalse(ok)
        self.assertTrue(detail)
        self.assertEqual(self.read(), before)

    def test_no_temp_file_is_left_behind(self):
        srv.write_posts([{"slug": "a"}], self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class TestTheRealStore(unittest.TestCase):
    """The owner's live content: STRUCTURE only, never a count or a specific slug."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.copy = os.path.join(self.tmp, "posts.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_has_the_expected_shape(self):
        posts = srv.read_posts(srv.POSTS_JSON)
        self.assertIsInstance(posts, list)
        for p in posts:
            self.assertTrue(p["slug"])
            self.assertTrue(p["title"])
            self.assertTrue(p["cover"].startswith("assets/"))
            self.assertIsInstance(p["gallery"], list)
            self.assertIsInstance(p["related"], list)
            self.assertEqual(list(p), list(srv.POST_FIELDS))     # canonical order on disk

    def test_real_store_round_trips_byte_identically(self):
        shutil.copy(srv.POSTS_JSON, self.copy)
        before = open(self.copy, encoding="utf-8").read()
        ok, detail = srv.write_posts(srv.read_posts(self.copy), self.copy)
        self.assertTrue(ok, detail)
        self.assertEqual(open(self.copy, encoding="utf-8").read(), before)


if __name__ == "__main__":
    unittest.main()
