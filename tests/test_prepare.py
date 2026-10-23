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
logs = builder.PATCHES["proton-logs"]
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
        }
        for name, text in preserved.items():
            (target / name).write_text(text)
        (target / "droiddeck-game-env").write_text(
            'def main():\n'
            '    launch, launch_env = select_sync(command, env, sync)\n'
            '    if launch[0] != command[0]:\n'
            '            record_stock(sync, launch, launch_env)\n'
            '            os.execvpe(launch[0], launch, launch_env)\n'
            '    os.execvpe(launch[0], launch, launch_env)\n')
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

    def test_changed_proton_launch_hook_stops_preparation_without_partial_patch(self):
        script = self.source / "tools/linuxfs/overlay/usr/local/bin/droiddeck-game-env"
        script.parent.mkdir(parents=True)
        text = 'def main():\n    upstream_launch_changed()\n'
        script.write_text(text)
        with self.assertRaisesRegex(RuntimeError, "session Proton log integration"):
            logs.patch_proton_logs(self.source)
        self.assertEqual(script.read_text(), text)

if __name__ == "__main__":
    unittest.main()

