"""Offline module contracts; no production services or credentials."""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import config as configmod
import inject
import providers
import store as storemod
import t4
import t4_service
import tvbox

SOURCE = {"id": "remote", "name": "Test T4", "api": "http://t4.test/api/demo?token=test",
          "kind": "t4", "enabled": True, "searchable": True}

def detail_row(order=False):
    return {"vod_id": "movie-1", "vod_name": "Test Series", "type_name": "电视剧",
            "vod_play_from": "B$$$A" if order else "A$$$B",
            "vod_play_url": ("第02集$b2#第01集$b1$$$第01集$a1#第02集$a2" if order else
                             "第01集$a1#第02集$a2$$$预告$trailer#第02集$b2#第01集$b1")}



class T4StateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            patch.object(configmod, "_STATE", Path(self.temp.name) / "config.json"),
            patch.object(storemod, "_STATE", Path(self.temp.name) / "titles.json"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.store = storemod.Store()
        self.addCleanup(self.store.close)
        self.service = t4_service.T4Service(self.store, self.config)
        self.source = dict(SOURCE)
        cfg = self.config.get()
        cfg["sources"] = [self.source]
        self.config.save({"sources": cfg["sources"]})


    def remember_card(self):
        card = t4.map_title({"vod_id": "movie-1", "vod_name": "Test Series"}, self.source)
        brief = inject.brief_of(card)
        self.store.remember_merged([brief])
        return brief["guid"]


    def test_classification_and_import_persistence(self):
        sites = [
            {"type": 4, "key": "pan", "name": "115云盘", "api": SOURCE["api"],
             "searchable": 0, "filterable": 1, "indexs": 0},
            {"type": 4, "key": "off", "name": "Disabled", "api": SOURCE["api"] + "&v=2",
             "enabled": False},
            {"type": 3, "key": "push", "name": "Push", "api": "csp_Push"},
            {"type": 4, "name": "Bad", "api": "file://tmp"}]
        groups = tvbox.classify_subscription(sites)
        self.assertEqual(len(groups["t4"]), 2)
        self.assertFalse(groups["t4"][0]["searchable"])
        self.assertFalse(groups["t4"][1]["enabled"])
        self.assertFalse(groups["cms"])
        report = self.config.import_t4_sources(groups["t4"])
        self.assertEqual(report["added"], 2)
        self.assertEqual(self.config.import_t4_sources(groups["t4"])["duplicate"], 2)
        reloaded = configmod.Config()
        imported = [s for s in reloaded.all_sources() if s.get("kind") == "t4"]
        self.assertEqual(len(imported), 3)
        self.assertTrue(next(s for s in imported if s.get("site_key") == "pan")["filterable"])
        self.assertFalse(next(s for s in imported if s.get("site_key") == "pan")["indexs"])


    def test_full_subscription_import_preserves_existing_sources_and_switches(self):
        for count in (172, 179):
            with self.subTest(count=count):
                self.config.save({"sources": [self.source]})
                sources = [tvbox.classify_site({
                    "type": 4, "key": f"plugin-{i}", "name": f"Plugin {i}",
                    "api": f"http://t4.test/t4/fm/env-at/plugin-{i}/",
                    "searchable": 0 if i % 7 == 0 else 1,
                    "indexs": 0 if i % 11 == 0 else 1,
                })[1] for i in range(count)]
                first = self.config.import_t4_sources(sources[:8])
                self.assertEqual(first["added"], 8)
                old = self.config.all_sources()
                existing = next(s for s in old if s.get("site_key") == "plugin-2")
                existing["enabled"] = False
                existing["name"] = "My existing name"
                self.config.save({"sources": old})
                report = self.config.import_t4_sources(sources)
                self.assertEqual(report["added"], count - 8)
                self.assertEqual(report["duplicate"], 8)
                repeated = self.config.import_t4_sources(sources)
                self.assertEqual((repeated["added"], repeated["duplicate"]), (0, count))
                reloaded = configmod.Config()
                rows = [s for s in reloaded.all_sources() if s.get("site_key")]
                self.assertEqual(len(rows), count)
                self.assertEqual(len({s["id"] for s in rows}), count)
                self.assertEqual([s["site_key"] for s in rows],
                                 [f"plugin-{i}" for i in range(count)])
                self.assertFalse(rows[2]["enabled"])
                self.assertEqual(rows[2]["name"], "My existing name")
                self.assertFalse(rows[0]["searchable"])
                self.assertFalse(rows[0]["indexs"])
                self.assertTrue(any(s["id"] == self.source["id"]
                                    for s in reloaded.all_sources()))
