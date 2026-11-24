from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageChops

from native_replay import _load, write_demo
from replay import ArenaRecording, KEY_INTERVAL
from replay_viewer import ReplayReader


class ReplayTests(unittest.TestCase):
    def test_motion_compression_seek_and_keyframe_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = ArenaRecording(Path(directory), {"matchId":"motion"})
            rec.encoder.compact_motion = True
            first = Image.new("RGB",(240,144),"#1b3d77")
            second = ImageChops.invert(first)
            rec.store_picture(first, 0)
            rec.store_picture(second, 33_333)
            rec.store_picture(second, 66_666)
            rec.store_picture(first, KEY_INTERVAL+1)
            rec.finish({"winner":2})
            reader = ReplayReader(rec.path)
            try:
                self.assertEqual(reader.verify(), 4)
                self.assertEqual(reader.frame_at(KEY_INTERVAL+1).tobytes(),first.tobytes())
                sampled = reader.frame_at(66_666).getpixel((100,70))
                self.assertLess(sum(abs(a-b) for a,b in zip(sampled,second.getpixel((100,70)))), 8)
            finally:
                reader.close()

    def test_offline_pixels_seek_audio_and_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = ArenaRecording(Path(directory), {"matchId": "sample", "resourceFingerprint": "A"})
            recorder.append({"type": "setup", "matchId": "sample", "map": "gense"})
            recorder.append({"type":"frame","tick":10,"players":[{},{}]})
            first = Image.new("RGB", (192, 128), (25, 100, 125))
            ImageDraw.Draw(first).rectangle((32,18,124,88), fill="yellow")
            second = ImageChops.offset(first, 9, 2)
            third = ImageChops.invert(second)
            recorder.store_picture(first, 0)
            recorder.store_picture(second, 33_333)
            recorder.store_picture(third, 66_666)
            recorder.store_audio(0, b"\x12\x00\x34\x00" * 1024)
            recorder.append({"type":"applied","tick":11,"p1":["right"],"p2":["attack"]})
            recorder.append({"type":"result","winner":1})
            recorder.finish({"winner":1})
            reader = ReplayReader(recorder.path)
            try:
                self.assertEqual(reader.verify(), 3)
                self.assertEqual(reader.frame_at(66_666).tobytes(), third.tobytes())
                self.assertEqual(reader.frame_at(0).tobytes(), first.tobytes())
                self.assertEqual(reader.frame_at(33_333).tobytes(), second.tobytes())
                self.assertEqual(reader.audit()["audioSamples"], 1024)
            finally:
                reader.close()
            metadata, tape = _load(write_demo(recorder.path))
            self.assertEqual(metadata["resourceFingerprint"], "A")
            self.assertEqual(tape.inputs[0]["offset"], 1)
            self.assertEqual(tape.winner, 1)
