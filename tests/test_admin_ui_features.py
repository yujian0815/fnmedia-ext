"""后台管理页结构回归：长列表折叠与272管理功能，不涉及播放策略。"""
from __future__ import annotations

import re
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import admin_ui


class AdminUiFeatureTests(unittest.TestCase):
    def test_unique_ids_and_layout_preserved(self):
        html = admin_ui.ADMIN_HTML
        ids = re.findall(r'id="([A-Za-z0-9_]+)"', html)
        self.assertEqual({k: v for k, v in Counter(ids).items() if v > 1}, {})
        self.assertEqual(len(re.findall(r'<section class="page', html)), 9)
        for page in ("overview", "library", "sources", "quality", "importsub", "search", "about"):
            self.assertIn(f'id="page-{page}"', html)

    def test_long_resource_list_collapse_and_filter(self):
        html = admin_ui.ADMIN_HTML
        for token in ('id="srcCollapseBar"', 'id="srcCollapseTxt"', 'id="srcFilter"',
                      'const PREVIEW=12', 'view=srcs.slice(0,PREVIEW)',
                      'function toggleSrcCollapse()', 'function toggleBox(',
                      'id="onlineBox"'):
            self.assertIn(token, html)
        self.assertIn('collapsed&&!q&&srcs.length>PREVIEW', html)
        self.assertIn('tr[data-id]', html)

    def test_cms_proxy_cache_t4_management_present(self):
        html = admin_ui.ADMIN_HTML
        for token in ('data-edit', 'function openSrcEdit(', 'function saveSrcEdit(',
                      'function exportCmsSources(', 'function importCmsFile(',
                      'id="srcEditMask"', 'id="srcProxyUrl"', 'function initSrcProxy(',
                      'id="cacheGrid"', 'function loadCacheStatus(', 'function clearCaches(',
                      'id="t4Enabled"', 'function saveT4Cfg(',
                      'function sourceProbeStart(', 'function sourceProbeCancel('):
            self.assertIn(token, html)

    def test_android_runtime_ui_removed_and_sidebar_scrolls(self):
        html = admin_ui.ADMIN_HTML
        for token in ('id="page-tvbox"', 'id="ovAndroid"', 'id="naPill"',
                      'sidecarInstall(', 'loadSpiderMgmt(', 'redroid', 'no-android'):
            self.assertNotIn(token, html)
        # 非安卓能力保留：DRPY/Node、网盘和 CMS 管理不被误删。
        self.assertIn('drpyProbe', html)
        self.assertIn('id="page-pan"', html)
        # 侧栏导航在窗口高度不足时自身滚动，不依赖最大化窗口。
        for token in ('flex:1 1 auto;min-height:0', 'overflow-y:auto',
                      'overscroll-behavior:contain', 'scrollbar-width:thin'):
            self.assertIn(token, html)

    def test_272_button_system_covers_real_button_elements(self):
        html = admin_ui.ADMIN_HTML
        self.assertIn('.btn,main button,.main button', html)
        for kind in ('primary', 'success', 'danger', 'warn', 'ghost'):
            self.assertIn(f'button.{kind}', html)
        self.assertIn('main button:focus-visible', html)


if __name__ == "__main__":
    unittest.main()
