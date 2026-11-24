from pathlib import Path
import tempfile
import unittest

from replay_files import files_for_selected_replays


class ReplayFilesTests(unittest.TestCase):
    def test_groups_selected_archive_demo_report_and_journal_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "test.v1.bvnreplay"
            demo = root / "test.v1.bvndemo"
            report = root / "test.v1.bvnreport.json"
            journal = root / "test.v1.bvnreplay-journal"
            other = root / "other.bvnreplay"
            for f in (archive, demo, report, journal, other):
                f.touch()
            self.assertEqual(set(files_for_selected_replays(root, [archive, demo])),
                             {archive.resolve(), demo.resolve(), report.resolve(), journal.resolve()})

    def test_rejects_external_replays_and_non_replay_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            root = folder / "data"
            root.mkdir()
            outside = folder / "external.bvnreplay"
            other = root / "series.json"
            for f in (outside, other):
                f.touch()
                with self.assertRaises(ValueError):
                    files_for_selected_replays(root, [f])

    def test_returns_the_data_demo_when_its_picture_file_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = root / "game.bvndemo"
            demo.touch()
            self.assertEqual(files_for_selected_replays(root, [demo]), [demo.resolve()])
