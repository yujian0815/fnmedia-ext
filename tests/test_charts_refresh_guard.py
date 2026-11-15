"""热门榜单刷新防异常缩水回归（不访问网络）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import charts


def _rows(n):
    return [{"id": str(i), "guid": f"g{i}"} for i in range(n)]


class RefreshGuardTests(unittest.TestCase):
    def test_empty_response_keeps_old_library(self):
        old = {"movie": _rows(160), "tv": _rows(160),
               "anime": _rows(160), "variety": _rows(140)}
        new = {"movie": _rows(10), "tv": _rows(20),
               "anime": [], "variety": _rows(20)}
        guarded, preserved = charts._guard_refresh_charts(new, old)
        self.assertEqual(len(preserved), 4)
        self.assertEqual({c: len(v) for c, v in guarded.items()},
                         {c: len(v) for c, v in old.items()})

    def test_partial_shrink_only_keeps_affected_library(self):
        old = {"movie": _rows(160), "tv": _rows(160),
               "anime": _rows(160), "variety": _rows(140)}
        new = {"movie": _rows(160), "tv": _rows(150),
               "anime": [], "variety": _rows(140)}
        guarded, preserved = charts._guard_refresh_charts(new, old)
        self.assertEqual([p[0] for p in preserved], ["anime"])
        self.assertEqual(len(guarded["anime"]), 160)
        self.assertEqual(len(guarded["movie"]), 160)
        self.assertEqual(len(guarded["tv"]), 150)

    def test_normal_change_accepted(self):
        old = {"movie": _rows(160), "tv": _rows(20),
               "anime": _rows(10), "variety": _rows(0)}
        new = {"movie": _rows(100), "tv": _rows(20),
               "anime": _rows(8), "variety": _rows(12)}
        guarded, preserved = charts._guard_refresh_charts(new, old)
        self.assertEqual(preserved, [])
        self.assertEqual(guarded, new)

    def test_small_library_only_zero_is_rejected(self):
        old = {"movie": _rows(8), "tv": _rows(0),
               "anime": _rows(0), "variety": _rows(0)}
        new = {"movie": _rows(4), "tv": _rows(5),
               "anime": _rows(0), "variety": _rows(0)}
        guarded, preserved = charts._guard_refresh_charts(new, old)
        self.assertEqual(preserved, [])
        self.assertEqual(len(guarded["movie"]), 4)


if __name__ == "__main__":
    unittest.main()
