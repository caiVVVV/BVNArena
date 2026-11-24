import contextlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import entrants
    import referee
except ImportError:
    entrants = referee = None


class FakeGame:
    pid = 12345

    def __init__(self, outcomes):
        self.catalog = {"type": "catalog", "fighters": ["f1","f2","f3","f4"],
                        "assists": ["a1","a2"], "maps": ["arena"], "teamSize": 3}
        self.outcomes = iter(outcomes)
        self.events = []
        self.setups = []
        self.inputs = []

    def send(self, msg):
        if msg["type"] == "setup":
            self.setups.append(msg)
            self.events = [{"type":"result","winner":1,"matchId":"previous-attempt"},
                           {"type": "frame", "tick": 10, "matchId": msg["matchId"], "players": [
                {"x": 20, "hp": 90, "hpMax": 100}, {"x": 70,"hp": 70,"hpMax":100}]}]
        elif msg["type"] == "input":
            self.inputs.append(msg)
            self.events.append({**msg,"type":"applied", "p1":msg.get("p1", [])})
            winner = next(self.outcomes)
            if winner == "drop":
                self.events.append(ConnectionError("test disconnect"))
            else:
                self.events.append({"type":"result","winner":winner,"matchId":msg["matchId"]})

    def receive(self, deadline):
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    def has_newer_frame(self, match_id, tick):
        return False


class Recording:
    def __init__(self, folder, metadata):
        self.path = folder / (metadata["matchId"] + ".bvnreplay")
        self.events = []

    def bind_game(self, pid):
        self.pid = pid

    def append(self, event):
        self.events.append(event)

    def finish(self, result):
        self.events.append(result)

    def interrupt(self, reason):
        self.events.append(reason)


class RefereeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(referee, "referee core and entrant runner must be importable")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.a = ROOT / "SDK" / "Examples" / "Blaze"
        self.b = ROOT / "SDK" / "Examples" / "Tide"

    def run_fake(self, outcomes, practice=False, **options):
        game = FakeGame(outcomes)
        self.game = game
        stop = threading.Event()
        with patch.object(referee, "_open_game", return_value=contextlib.nullcontext(game)), \
             patch.object(referee, "verify_game"), \
             patch.object(referee, "_new_recording", side_effect=Recording), \
             patch.object(referee, "_fingerprint", return_value="test"), \
             patch.object(referee.secrets, "randbelow", return_value=0):
            return referee.run_series(self.folder, self.a, None if practice else self.b,
                                      "arena", self.folder, lambda x: None, stop, **options)

    def test_draw_preserves_seats_and_full_team_wins_rotate_to_four(self):
        result = self.run_fake([0,1,2,1,2])
        self.assertEqual(result["score"], [4,0])
        self.assertEqual(len(self.game.setups), 5)
        seats = [s["p1"]["name"] for s in self.game.setups]
        self.assertEqual(seats, [seats[0],seats[0],seats[2],seats[0],seats[2]])
        self.assertNotEqual(seats[0], seats[2])
        self.assertEqual(self.game.setups[-1]["score"], [0,3])
        self.assertTrue(all(len(set(s["p1"]["roster"])) == 3 for s in self.game.setups))
        self.assertTrue(all(x["tick"] == 10 for x in self.game.inputs))
        self.assertTrue(any(x["p1"] for x in self.game.inputs))

    def test_disconnect_cannot_score_and_reconnect_preserves_previous_round(self):
        result = self.run_fake([1, "drop", 2, 1, 2])
        self.assertEqual(result["score"], [4,0])
        self.assertEqual(self.game.setups[2]["score"], [0,1])

    def test_seven_decided_games_reaches_four_three(self):
        result = self.run_fake([1,1,1,1,1,1,1])
        self.assertEqual(result["score"], [4,3])
        self.assertEqual(len(self.game.setups), 7)

    def test_persistent_disconnect_then_restart(self):
        with self.assertRaises(ConnectionError):
            self.run_fake([1, "drop", "drop", "drop"])
        ledger, = (self.folder / "series").glob("*.json")
        legacy = json.loads(ledger.read_text(encoding="utf-8"))
        legacy.pop("mode", None)
        ledger.write_text(json.dumps(legacy), encoding="utf-8")
        result = self.run_fake([2,1,2])
        self.assertEqual(result["score"], [4,0])
        self.assertEqual(self.game.setups[0]["score"], [0,1])

    def test_practice_one_game_cpu_no_ranked_score(self):
        result = self.run_fake([2], practice=True)
        self.assertEqual(len(self.game.setups), 1)
        self.assertTrue(self.game.setups[0]["p2"]["practiceCPU"])
        self.assertEqual(self.game.inputs[0]["p2"], [])
        self.assertTrue(result["practice"])

    def run_human_fake(self, game, opponent, **options):
        with patch.object(referee, "_open_game", return_value=contextlib.nullcontext(game)), \
             patch.object(referee, "verify_game"), \
             patch.object(referee, "_new_recording", side_effect=Recording), \
             patch.object(referee, "_fingerprint", return_value="test"):
            return referee.run_human_match(
                self.folder, opponent, "arena", self.folder, lambda _: None, threading.Event(), **options)

    def test_human_orders_roster_and_only_p2_receives_lineups_after_draft(self):
        received = self.folder / "messages.jsonl"
        manifest = self.folder / "entrant.json"
        manifest.write_text(json.dumps({"name":"opponent","color":"#123456","engine":"bundled-python",
                                        "script":"bot.py"}), encoding="utf-8")
        (self.folder / "bot.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "for line in sys.stdin:\n"
            " q=json.loads(line)\n"
            " with Path(__file__).with_name('messages.jsonl').open('a',encoding='utf-8') as log:\n"
            "  log.write(line)\n"
            " answer=dict(type='pick',roster=['f1','f2','f3'],assist='a1') if q['type']=='draft' "
            "else dict(type='action',tick=q['tick'],buttons=['attack'])\n"
            " print(json.dumps(dict(answer,requestId=q['requestId'])),flush=True)\n", encoding="utf-8")

        class HumanGame(FakeGame):
            def send(self, msg):
                if msg["type"] == "setup":
                    self.setups.append(msg)
                    self.events = [{"type":"frame", "tick":10, "matchId":msg["matchId"],
                                    "players":[{"id":"f4", "name":"首发"}, {"id":"f1"}]}]
                elif msg["type"] == "input":
                    self.inputs.append(msg)
                    self.events = ([{"type":"frame", "tick":10, "matchId":msg["matchId"],
                                     "players":[{"id":"stale"}, {"id":"f1"}]},
                                    {"type":"frame", "tick":11, "matchId":msg["matchId"],
                                     "players":[{"id":"f2", "name":"替补"}, {"id":"f1"}]}]
                                   if msg["tick"] == 10 else
                                   [{"type":"result", "winner":2, "matchId":msg["matchId"]}])

        game = HumanGame([])
        lang = self.folder / "lang"
        lang.mkdir()
        (lang / "zh-CN.json").write_text(json.dumps({"name":{
            "fighter":{"f4":{"name":"先发"},"f2":{"name":"次发"},"f1":{"name":"末发"}},
            "assist":{"a2":{"name":"援护"}}}}, ensure_ascii=False), encoding="utf-8")
        result = self.run_human_fake(game, manifest, roster=["f4","f2","f1"], assist="a2")
        self.assertEqual(result["score"], [0,1])
        self.assertTrue(result["practice"])
        self.assertIsNone(result["ledger"])
        self.assertEqual(len(game.setups), 1)
        self.assertTrue(game.setups[0]["p1"]["human"])
        self.assertEqual(game.setups[0]["p1"]["roster"], ["f4","f2","f1"])
        self.assertEqual(game.setups[0]["p1"]["assist"], "a2")
        self.assertEqual(game.setups[0]["p2"]["name"], "opponent")
        self.assertTrue(all("p1" not in event and event["p2"] == ["attack"] for event in game.inputs))

        draft, first, second = [json.loads(line) for line in received.read_text(encoding="utf-8").splitlines()]
        self.assertEqual((draft["type"], first["type"], second["type"]), ("draft", "frame", "frame"))
        self.assertTrue(all(q["seat"] == 2 for q in (draft, first, second)))
        self.assertNotIn("lineups", draft)
        self.assertNotIn("opponent", draft)
        self.assertTrue(all("roster" not in team and "assist" not in team for team in draft["teams"]))
        self.assertEqual(first["lineups"][0], {"roster":["f4","f2","f1"],"assist":"a2",
                                              "rosterNames":["先发","次发","末发"],"assistName":"援护"})
        self.assertEqual(first["lineups"][1]["roster"], ["f1","f2","f3"])
        self.assertEqual(first["opponent"], dict(first["lineups"][0], activeId="f4",activeName="首发"))
        self.assertEqual(second["opponent"], dict(first["lineups"][0], activeId="f2",activeName="替补"))
        self.assertEqual(second["lineups"], first["lineups"])

    def test_human_can_start_without_overriding_default_fighters_or_assist(self):
        game = FakeGame([1])
        result = self.run_human_fake(game, self.b)
        self.assertEqual(result["score"], [1,0])
        self.assertEqual(len(game.setups), 1)
        self.assertEqual(game.setups[0]["p1"]["roster"], ["f1","f2","f3"])
        self.assertEqual(game.setups[0]["p1"]["assist"], "a1")

    def test_human_invalid_pick_is_rejected_before_draft_or_setup(self):
        game = FakeGame([])
        with self.assertRaisesRegex(ValueError, "不能重复选角"):
            self.run_human_fake(game, self.b, roster=["f1","f1","f2"], assist="a1")
        self.assertEqual(game.setups, [])

    def test_new_series_and_practice_use_unlimited_round_time(self):
        self.run_fake([1,2,1,2])
        self.assertTrue(all(setup.get("fightTime")==-1 for setup in self.game.setups))
        self.run_fake([1], practice=True)
        self.assertEqual(self.game.setups[0].get("fightTime"), -1)

    def test_entrant_file_passed_to_launcher_counts_toward_series_identity(self):
        folder = self.folder / "launcher entrant"
        folder.mkdir()
        (folder / "runner.exe").write_bytes(b"runner")
        manifest = {"name":"A", "color":"#456789", "engine":"executable",
                    "executable":"runner.exe", "args":["--entrant-script","bot.py"]}
        (folder / "entrant.json").write_text(json.dumps(manifest),encoding="utf-8")
        bot = folder / "bot.py"
        bot.write_text("print('first')",encoding="utf-8")
        old = entrants.load_team(folder).fingerprint
        bot.write_text("print('second')",encoding="utf-8")
        self.assertNotEqual(entrants.load_team(folder).fingerprint, old)

    def test_skip_cut_in_is_opt_in_and_does_not_resume_another_rule_set(self):
        with self.assertRaises(ConnectionError):
            self.run_fake([1, "drop", "drop", "drop"])
        self.assertTrue(all(not s.get("skipCutIn") for s in self.game.setups))
        result = self.run_fake([1, 2, 1, 2], skip_cut_in=True)
        self.assertEqual(result["score"], [4, 0])
        self.assertEqual(self.game.setups[0]["score"], [0, 0])
        self.assertTrue(all(s["skipCutIn"] for s in self.game.setups))

    def test_draw_limit_pauses_and_allows_retry(self):
        with self.assertRaisesRegex(RuntimeError, "平局"):
            self.run_fake([0,0,0])
        self.assertEqual(self.run_fake([1,2,1,2])["score"], [4,0])

    def test_invalid_and_ambiguous_button_sets(self):
        self.assertEqual(entrants.buttons(["super","left","left"]), ["super","left"])
        for value in (["left","right"], ["dash","cheat"], None, {"skill":1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                entrants.buttons(value)

    def test_reject_stale_or_guessed_bot_reply(self):
        manifest = self.folder / "team.json"
        manifest.write_text(json.dumps({"name":"test","color":"#123456","engine":"bundled-python","script":"bot.py"}))
        (self.folder/"bot.py").write_text(
            "import json,sys\nfor line in sys.stdin:\n"
            " q=json.loads(line)\n"
            " for key,tick,actions in [('obsolete',9,['attack']), (q['requestId'],11,['super']),"
            "(q['requestId'],10,['jump'])]:\n"
            "  print(json.dumps(dict(type='action',tick=tick,requestId=key,buttons=actions)),flush=True)\n")
        runner = entrants.BotProcess(entrants.load_team(manifest))
        self.addCleanup(runner.close)
        result = entrants.exchange([runner,None], [{"type":"frame","tick":10},None], 2.0)
        self.assertEqual(result[0]["buttons"], ["jump"])


if __name__ == "__main__":
    unittest.main()
