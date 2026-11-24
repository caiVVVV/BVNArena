import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from installation import deploy, remove_installation, list_human_choices, list_maps, export_training_kit, InstallError
import zipfile


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = Path(self.temp.name) / "含空格 game"
        (self.game / "assets/plugin").mkdir(parents=True)
        (self.game / "assets/map").mkdir()
        (self.game / "assets/map/gense.swf").touch()
        (self.game / "lang").mkdir()
        (self.game / "lang/zh-CN.json").write_text('{"map":{"gense":"现世"}}', encoding="utf-8")
        (self.game / "version.json").write_text('{"version":"3.8.8.5"}')
        (self.game / "Bleach vs. Naruto.exe").write_bytes(b"game")
        support = patch("installation.SUPPORTED_GAME_SHA256", hashlib.sha256(b"game").hexdigest(), create=True)
        support.start()
        self.addCleanup(support.stop)
        self.bin = Path(self.temp.name) / "arena.exe"
        self.bin.write_bytes(b"manager1")
        self.assets = Path(self.temp.name) / "payload"
        (self.assets / "bridge").mkdir(parents=True)
        (self.assets / "bridge/ArenaBridge.swf").write_bytes(b"swf")
        (self.assets / "SDK").mkdir()
        (self.assets / "SDK/guide.txt").write_text("guide")

    def test_repeat_install_and_uninstall_preserves_user_files(self):
        for _ in range(2):
            deploy(self.game, self.bin, self.assets)
        user = self.game / "AIArena/data/series.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_bytes(b"user")
        remove_installation(self.game)
        self.assertEqual(user.read_bytes(), b"user")
        self.assertFalse((self.game / "assets/plugin/ArenaBridge.swf").exists())
        self.assertEqual((self.game / "Bleach vs. Naruto.exe").read_bytes(), b"game")

    def test_foreign_plugin_never_overwritten(self):
        other = self.game / "assets/plugin/ArenaBridge.swf"
        other.write_bytes(b"mine")
        with self.assertRaises(InstallError):
            deploy(self.game, self.bin, self.assets)
        self.assertEqual(other.read_bytes(), b"mine")

    def test_uninstall_preserves_modified_sdk(self):
        deploy(self.game, self.bin, self.assets)
        guide = self.game / "AIArena/SDK/guide.txt"
        guide.write_bytes(b"mine")
        remove_installation(self.game)
        self.assertEqual(guide.read_bytes(), b"mine")

    def test_map_from_game_resources(self):
        self.assertEqual(list_maps(self.game), [("gense", "现世")])

    def test_human_choices_use_installed_selectable_assets_without_catalog_cache(self):
        fighters = self.game / "assets/fighter"
        (fighters / "fz").mkdir(parents=True)
        (fighters / "xb").mkdir()
        for file in ("ichigo.f", "naruto.f", "sasuke.f", "xb/aizen_captain.f", "never_selectable.f",
                     "fz/kon.f", "fz/never_selectable.f"):
            (fighters / file).touch()
        (self.game / "lang/zh-CN.json").write_text(
            '{"name":{"fighter":{"ichigo":{"name":"黑崎·一护"}},"assist":{"kon":{"name":"魂"}}}}',
            encoding="utf-8")
        fighters, assists = list_human_choices(self.game)
        self.assertEqual(dict(fighters), {"aizen_captain":"aizen_captain", "ichigo":"黑崎·一护",
                                          "naruto":"naruto", "sasuke":"sasuke"})
        self.assertEqual(assists, [("kon", "魂")])

    def test_unsupported_version_has_no_side_effects(self):
        (self.game / "version.json").write_text('{"version":"3.8"}')
        with self.assertRaises(InstallError):
            deploy(self.game, self.bin, self.assets)
        self.assertFalse((self.game / "AIArena").exists())

    def test_different_executable_with_same_version_is_rejected(self):
        (self.game / "Bleach vs. Naruto.exe").write_bytes(b"different build")
        with self.assertRaises(InstallError):
            deploy(self.game, self.bin, self.assets)
        self.assertFalse((self.game / "AIArena").exists())

    def test_export_contains_same_game_and_sdk_without_match_history(self):
        deploy(self.game, self.bin, self.assets)
        history = self.game / "AIArena/data/private.json"
        history.parent.mkdir()
        history.write_text("private")
        dest = Path(self.temp.name) / "kit.zip"
        export_training_kit(self.game, dest)
        with zipfile.ZipFile(dest) as archive:
            names = archive.namelist()
            self.assertIn("BVN/AIArena/SDK/guide.txt", names)
            self.assertIn("BVN/Bleach vs. Naruto.exe", names)
            self.assertNotIn("BVN/AIArena/data/private.json", names)

    def test_bundled_entrant_survives_an_update_from_the_standard_installer(self):
        bundle = self.assets / "Entrants"
        bot = bundle / "Model A/bot.py"
        bot.parent.mkdir(parents=True)
        bot.write_text("print('ready')", encoding="utf-8")
        deploy(self.game, self.bin, self.assets)
        installed_bot = self.game / "AIArena/Entrants/Model A/bot.py"
        self.assertEqual(installed_bot.read_bytes(), bot.read_bytes())
        bundle.rename(Path(self.temp.name) / "optional-entrant-payload")
        deploy(self.game, self.bin, self.assets)
        self.assertEqual(installed_bot.read_text("utf-8"), "print('ready')")

    def test_training_export_omits_competition_entries_and_their_manifest_entries(self):
        bot = self.assets / "Entrants/Model A/bot.py"
        bot.parent.mkdir(parents=True)
        bot.write_text("print('ready')", encoding="utf-8")
        deploy(self.game, self.bin, self.assets)
        self.assertTrue((self.game / "AIArena/Entrants/Model A/bot.py").is_file())
        dest = Path(self.temp.name) / "kit.zip"
        export_training_kit(self.game, dest)
        with zipfile.ZipFile(dest) as archive:
            self.assertNotIn("BVN/AIArena/Entrants/Model A/bot.py", archive.namelist())
            manifest = json.loads(archive.read("BVN/AIArena/install.json"))
            self.assertNotIn("AIArena/Entrants/Model A/bot.py", manifest["files"])
            self.assertIn("AIArena/SDK/guide.txt", manifest["files"])
