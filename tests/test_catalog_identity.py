"""海量目录元数据身份索引持久化回归（不涉及播放绑定）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import catalog


class CatalogIdentityTests(unittest.TestCase):
    def test_persist_and_reload_without_playback_lines(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(catalog, "IDENTITY_STATE", Path(td) / "identity.json"):
            c = catalog.Catalog()
            brief = {"guid": "kg", "title": "海量片", "year": "2026",
                     "type_name": "动作片", "poster": "p.jpg",
                     "overview": "简介", "catalog_cat": "movie",
                     "catalog_scope": "__agg__", "catalog_source": "lzi",
                     "lines": {"lzi": [{"url": "must-not-persist"}]}}
            c._register_identity([brief])
            c._save_identity(force=True)
            self.assertTrue(catalog.IDENTITY_STATE.exists())
            raw = catalog.IDENTITY_STATE.read_text()
            self.assertNotIn("must-not-persist", raw)
            self.assertNotIn("lines", raw)
            # 现有播放绑定查询不被轻量身份索引改变
            self.assertIsNone(c.find_brief_by_guid("kg"))

            restored = catalog.Catalog()
            row = restored.identity_of_guid("kg")
            self.assertEqual(row["catalog_scope"], "__agg__")
            self.assertEqual(row["title"], "海量片")
            self.assertNotIn("lines", row)

    def test_custom_scope_retained_for_whitelist_filter(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(catalog, "IDENTITY_STATE", Path(td) / "identity.json"):
            c = catalog.Catalog()
            c._register_identity([{"guid": "sg", "title": "自定义片",
                                   "catalog_scope": "custom_abc",
                                   "catalog_cat": "movie"}])
            c._save_identity(force=True)
            self.assertEqual(c.identity_of_guid("sg")["catalog_scope"],
                             "custom_abc")


if __name__ == "__main__":
    unittest.main()
