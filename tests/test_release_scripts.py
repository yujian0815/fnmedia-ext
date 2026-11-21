"""发布脚本的通道说明与应用中心恢复契约（只读静态回归）。"""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseScriptTests(unittest.TestCase):
    def test_beta_notes_are_forced_after_notes_selection(self):
        script = (ROOT / "tools/publish-public.sh").read_text(encoding="utf-8")
        selection = script.index('if [ -n "$NOTES_FILE" ]')
        override = script.index("# Beta 只提供统一预发布提示")
        release = script.index("gh release create")
        self.assertGreater(override, selection)
        self.assertLess(override, release)
        self.assertIn('if [ "$DO_PRERELEASE" -eq 1 ]; then', script[override:release])
        self.assertIn("printf 'Beta 预发布版本。\\n' > \"$NOTES\"", script)
        # Beta 继续跳过 Moo，正式版仍保留审查 notes 与 Moo 更新分支。
        self.assertIn("预发布版：跳过 moo.json 更新", script)
        self.assertIn('elif [ -f "$ROOT/tools/gen-moo-json.py" ]', script)

    def test_app_center_start_restores_enabled_service(self):
        script = (ROOT / "packaging/fpk/cmd/main").read_text(encoding="utf-8")
        self.assertIn("start)  systemctl enable --now fnmedia-ext.service", script)

    def test_release_guide_records_beta_policy(self):
        guide = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
        self.assertIn("### Beta 预发布", guide)
        self.assertIn("Beta 预发布版本。", guide)
        self.assertIn("即使误传 `--notes`", guide)
        self.assertIn("Beta 不更新 `moo.json`", guide)


if __name__ == "__main__":
    unittest.main()
