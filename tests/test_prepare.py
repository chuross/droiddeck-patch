import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare", ROOT / "tools/prepare.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
imports = builder.PATCHES["proton-import"]

class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        builder.git(self.source, "init", "-b", "main")
        builder.git(self.source, "config", "user.name", "Test")
        builder.git(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "base").write_text("base")
        self.commit("base")
        self.base = builder.git(self.source, "rev-parse", "HEAD").stdout.strip()

    def commit(self, message):
        builder.git(self.source, "add", ".")
        builder.git(self.source, "commit", "-m", message)
        return builder.git(self.source, "rev-parse", "HEAD").stdout.strip()

    def test_existing_runtime_receives_overlay_helpers(self):
        file = self.source / "app/src/main/java/com/droiddeck/launcher/session/SessionFiles.kt"
        file.parent.mkdir(parents=True)
        file.write_text('        val files = arrayOf(\n        )\n')
        builder.stage_overlay_assets(self.source)
        text = file.read_text()
        for helper in tuple(helper for module in builder.PATCHES.values() for helper in getattr(module, "HELPERS", ())):
            self.assertEqual(text.count('"usr/local/bin/' + helper + '" to'), 1)

    def test_changed_runtime_staging_fails_instead_of_shipping_unapplied_fix(self):
        file = self.source / "app/src/main/java/com/droiddeck/launcher/session/SessionFiles.kt"
        file.parent.mkdir(parents=True)
        file.write_text('changed staging implementation')
        with self.assertRaisesRegex(RuntimeError, "SessionFiles staging changed"):
            builder.stage_overlay_assets(self.source)

    def test_overlay_keeps_upstream_language_and_font_support(self):
        target = self.source / "tools/linuxfs/overlay/usr/local/bin"
        target.mkdir(parents=True)
        preserved = {
            "droiddeck-session": 'upstream app-selected Steam language\n',
            "droiddeck-steam-language": 'upstream language helper\n',
            "droiddeck-fonts": 'upstream Android font support\n',
            "steam-compatibility": 'upstream compatibility tools\n',
            "droiddeck-game-env": 'upstream game environment and Proton logging\n',
        }
        for name, text in preserved.items():
            (target / name).write_text(text)
        staging = self.source / "app/src/main/java/com/droiddeck/launcher/session/SessionFiles.kt"
        staging.parent.mkdir(parents=True)
        staging.write_text('        val files = arrayOf(\n        )\n')
        (self.source / "app/build.gradle").write_text('applicationId "com.droiddeck.launcher"\n')
        with patch.object(imports, "check"), patch.object(imports, "apply"):
            builder.overlay(self.source)
        for name, text in preserved.items():
            self.assertEqual((target / name).read_text(), text)
        self.assertNotIn("jp_fonts", (target / "droiddeck-game-env").read_text())
        self.assertIn('applicationId "com.droiddeck.launcher.japanese"',
                      (self.source / "app/build.gradle").read_text())

if __name__ == "__main__":
    unittest.main()

