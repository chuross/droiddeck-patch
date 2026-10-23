import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = os.environ.get("DROIDDECK_PATCH_SOURCE")


class Finished(BaseException):
    pass


@unittest.skipUnless(SOURCE, "set DROIDDECK_PATCH_SOURCE to a prepared checkout")
class LaunchIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = Path(SOURCE) / "tools/linuxfs/overlay/usr/local/bin/droiddeck-game-env"
        self.module = runpy.run_path(str(path))
        self.globals = self.module["main"].__globals__
        self.env = {"STEAM_COMPAT_DATA_PATH": str(self.root / "compatdata/123")}

    def run_launch(self, fallback):
        chosen = ["selected-proton", "waitforexitandrun", "game.exe"]
        attempts = []

        def helper(path):
            return {"launch": lambda command, env: None}

        def execute(binary, command, env):
            attempts.append((binary, command, env))
            if fallback and len(attempts) == 1:
                raise OSError("selected Proton executable unavailable")
            raise Finished()

        with patch.dict(self.globals, load_sync=lambda: {},
                        select_sync=lambda command, env, sync: (chosen, dict(env, BL_SYNC_PACK="selected")),
                        record_stock=lambda *args: None), \
             patch.object(self.globals["runpy"], "run_path", side_effect=helper), \
             patch.object(self.globals["os"], "execvpe", side_effect=execute), \
             patch.dict(os.environ, self.env, clear=True), \
             patch.object(self.globals["sys"], "argv",
                          ["droiddeck-game-env", "stock-proton", "waitforexitandrun", "game.exe"]), \
             patch.object(Path, "home", return_value=self.root):
            with self.assertRaises(Finished):
                self.module["main"]()
        return attempts


    def test_proton_logs_follow_each_session_and_preserve_manual_toggle(self):
        for session, flag in (("first session", "1"), ("next session", "0")):
            with self.subTest(session=session, flag=flag):
                directory = str(self.root / session)
                self.env.update(BL_DEBUG_DIR=directory, PROTON_LOG=flag,
                                PROTON_LOG_DIR=str(self.root / "old session"))
                attempts = self.run_launch(False)
                self.assertEqual(attempts[0][2]["PROTON_LOG_DIR"], directory)
                self.assertEqual(attempts[0][2]["PROTON_LOG"], flag)

    def test_logging_stays_disabled_by_default_in_selected_and_stock_proton(self):
        self.env["BL_DEBUG_DIR"] = str(self.root / "session")
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                attempts = self.run_launch(fallback)
                for _, _, env in attempts:
                    self.assertEqual(env["PROTON_LOG_DIR"], self.env["BL_DEBUG_DIR"])
                    self.assertNotIn("PROTON_LOG", env)

    def test_session_log_parent_is_used_when_debug_directory_is_unset(self):
        self.env.update(BL_LOG=str(self.root / "session/session.log"), PROTON_LOG="1")
        attempts = self.run_launch(True)
        for _, _, env in attempts:
            self.assertEqual(env["PROTON_LOG_DIR"], str(self.root / "session"))
            self.assertEqual(env["PROTON_LOG"], "1")

    def test_without_session_directory_existing_proton_destination_is_preserved(self):
        self.env.update(PROTON_LOG="1", PROTON_LOG_DIR=str(self.root / "custom"))
        attempts = self.run_launch(False)
        self.assertEqual(attempts[0][2]["PROTON_LOG_DIR"], self.env["PROTON_LOG_DIR"])

    def test_launched_process_writes_log_into_session_only_when_enabled(self):
        proton = self.root / "proton"
        proton.write_text(
            '#!' + sys.executable + '\n'
            'import os\nfrom pathlib import Path\n'
            'if os.environ.get("PROTON_LOG", "0") != "0":\n'
            '    (Path(os.environ["PROTON_LOG_DIR"]) / "steam-123.log").write_text("Proton log fixture")\n')
        proton.chmod(0o755)
        script = Path(SOURCE) / "tools/linuxfs/overlay/usr/local/bin/droiddeck-game-env"
        for name, flag in (("enabled session", "1"), ("disabled session", "0")):
            with self.subTest(flag=flag):
                directory = self.root / name
                directory.mkdir()
                environment = dict(os.environ, **self.env, HOME=str(self.root),
                                   BL_DEBUG_DIR=str(directory), PROTON_LOG=flag,
                                   PROTON_LOG_DIR=str(self.root / "stale session"))
                subprocess.run([sys.executable, str(script), str(proton), "waitforexitandrun", "game.exe"],
                               env=environment, check=True, capture_output=True)
                log = directory / "steam-123.log"
                if flag == "1":
                    self.assertEqual(log.read_text(), "Proton log fixture")
                else:
                    self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()


