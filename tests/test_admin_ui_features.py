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
        expected = {"overview", "library", "sources", "importsub", "pan",
                    "playback", "search", "network", "data", "about"}
        sections = set(re.findall(r'<section class="page[^>]*id="page-([^"]+)"', html))
        self.assertEqual(sections, expected)
        self.assertEqual(len(sections), 10)
        nav_pages = set(re.findall(r'data-page="([^"]+)"', html))
        self.assertEqual(nav_pages, expected)
        self.assertNotIn("page-quality", html)
        self.assertNotIn("page-xiaoya", html)
        # 移动端核心导航 + 更多面板覆盖全部页面，无不可达功能。
        self.assertIn('id="moreNav"', html)
        self.assertIn('id="moreNavBtn"', html)
        self.assertIn('function toggleMoreNav()', html)

    def test_cards_live_in_their_functional_sections(self):
        html = admin_ui.ADMIN_HTML

        def section(page):
            start = html.index(f'id="page-{page}"')
            end = html.find('<section class="page', start + 1)
            return html[start:end if end >= 0 else len(html)]

        for element, page in (("prioRows", "sources"), ("healthRows", "sources"),
                              ("drpyMgmt", "importsub"), ("nodePill", "importsub"),
                              ("panFrame", "pan"), ("jvmPill", "pan"),
                              ("lineFastest", "playback"), ("adfEnable", "playback"),
                              ("onlineFirst", "search"), ("subProxyUrl", "network"),
                              ("srcProxyUrl", "network"), ("updProxy", "network"),
                              ("onlineBox", "data"), ("cacheGrid", "data"),
                              ("updCur", "about"), ("panLibEnabled", "library")):
            with self.subTest(element=element, page=page):
                self.assertIn(f'id="{element}"', section(page))

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

    def test_subscription_import_and_legacy_sources_are_report_only_without_android(self):
        html = admin_ui.ADMIN_HTML
        for token in ('function renderAndroidSubGroup(', '主线无安卓 · 不导入',
                      '未写入源列表', 'Guard 加固源', '普通 csp_ 爬虫',
                      '安卓源不可用', 'runtime_unavailable', 'target.spider_src',
                      '另有 \'+androidN+\' 个安卓依赖源未导入'):
            self.assertIn(token, html)
        self.assertIn('CMS/T4 新增', html)
        self.assertIn('T4 选片源', html)
        self.assertIn('t4=d.t4||{}', html)
        self.assertNotIn('新增可直接使用', html)
        # 旧安卓管理/导入 JS 不得随新展示层复活。
        for token in ('function importFantaiying(', 'function subToggleGuard(',
                      'enable_guard:"smart"'):
            self.assertNotIn(token, html)

    def test_272_button_system_covers_real_button_elements(self):
        html = admin_ui.ADMIN_HTML
        self.assertIn('.btn,main button,.main button', html)
        for kind in ('primary', 'success', 'danger', 'warn', 'ghost'):
            self.assertIn(f'button.{kind}', html)
        self.assertIn('main button:focus-visible', html)


if __name__ == "__main__":
    unittest.main()
