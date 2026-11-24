import unittest
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from installation import MANIFEST, PLUGIN
from native_replay import MatchTape, compare_frame, matches_resource_fingerprint, resource_fingerprint


class NativeTests(unittest.TestCase):
    def test_groups_inputs_by_actual_engine_tick(self):
        data = [
            {"type":"setup","matchId":"abc","map":"gense"},
            {"type":"frame","tick":120,"round":1,"players":[{},{}]},
            {"type":"applied","tick":120,"requestedTick":119,"p1":["jump"],"p2":[]},
            {"type":"applied","tick":122,"p1":[],"p2":["attack"]},
            {"type":"result","winner":2}
        ]
        tape = MatchTape(data)
        self.assertEqual([e["offset"] for e in tape.inputs], [0,2])
        self.assertEqual(tape.winner, 2)

    def test_mismatch_detects_position_hp_and_state(self):
        frame = {"round":1,"players":[{"id":"a","x":5.1,"hp":90,"state":3},{"id":"b"}]}
        self.assertIsNone(compare_frame(frame,dict(frame)))
        other = {"round":1,"players":[{"id":"a","x":5.1,"hp":80,"state":3},{"id":"b"}]}
        self.assertIn("hp", compare_frame(frame,other))

    def test_mismatch_detects_a_differing_round_clock(self):
        frame = {"round":1,"map":"gense","time":20,"players":[{},{}]}
        self.assertIn("倒计时", compare_frame(frame,dict(frame,time=19)))

    def test_rejects_empty_unfinished_or_malformed_match(self):
        for events in ([],[{"type":"setup","map":"x"},{"type":"frame","tick":2}],):
            with self.assertRaises(ValueError):
                MatchTape(events)

    def test_accepts_old_bridge_only_when_current_assets_and_managed_bridge_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "version.json").write_text('{"version":"3.8.8.5"}')
            engine = root / "Bleach vs. Naruto.exe"
            engine.write_bytes(b"game binary")
            bridge = root / PLUGIN
            bridge.parent.mkdir(parents=True)
            before, after = b"old bridge", b"new bridge with playback badge"
            bridge.write_bytes(before)
            expected = resource_fingerprint(root)
            bridge.write_bytes(after)
            manifest = root / MANIFEST
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({"product":"BVN Arena", "files":{
                PLUGIN:hashlib.sha256(after).hexdigest()}}))
            with patch("native_replay.COMPATIBLE_BRIDGE_SHA256", (hashlib.sha256(before).hexdigest(),)):
                self.assertTrue(matches_resource_fingerprint(root,expected))
                bridge.write_bytes(b"an unmanaged version")
                self.assertFalse(matches_resource_fingerprint(root,expected))
                bridge.write_bytes(after)
                engine.write_bytes(b"changed game binary")
                self.assertFalse(matches_resource_fingerprint(root,expected))
