import json
from pathlib import Path
import tempfile
import unittest

import battle_report
from replay import ArenaRecording


def fighter(seat, id, name):
    return {"seat": seat, "id": id, "name": name}


P1 = fighter(1, "ichigo", "一护")
P2 = fighter(2, "naruto", "鸣人")
ASSIST = {"kind": "assist", "seat": 1, "id": "rukia", "name": "露琪亚",
          "move": "白雷", "confirmed": True}


def frame(tick, hp1, hp2, round=1, p2=P2):
    return {"type": "frame", "tick": tick, "round": round,
            "players": [dict(P1, hp=hp1), dict(p2, hp=hp2)]}


def damage(tick, before, after, source, target=P2, round=1):
    return {"type": "damage", "tick": tick, "round": round, "target": target,
            "source": source, "hpBefore": before, "hpAfter": after}


class BattleReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.recording = ArenaRecording(Path(self.directory), {
            "matchId": "recorded", "seriesId": "series", "map": "gense", "round": 3,
            "practice": True, "teams": [{"name": "训练机器人", "side": "A"},
                                         {"name": "游戏 CPU", "side": "B"}],
        })
        self.addCleanup(self.recording.interrupt, "测试清理")

    def export(self, *events, result=None, interrupted=None):
        for event in events:
            self.recording.append(dict(event, matchId=event.get("matchId", "recorded")))
        if interrupted is not None:
            self.recording.interrupt(interrupted)
        else:
            self.recording.finish(result or {"winner": 1, "reason": "team_elimination"})
        path = battle_report.write_report(self.recording.path)
        self.assertEqual(path, self.recording.path.with_suffix(".bvnreport.json"))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_runtime_hits_coexist_with_snapshots_and_confirm_a_ko(self):
        report = self.export(
            {"type": "setup", "p1": {"roster": ["ichigo"], "assist": "rukia"},
             "p2": {"roster": ["naruto"], "assist": "kon", "practiceCPU": True}},
            frame(10, 100, 100),
            {"type": "attack", "seat": 1, "fighter": P1, "move": "普通攻击", "tick": 11, "round": 1},
            damage(12, 100, 73, ASSIST), damage(12, 73, 55, ASSIST), frame(12, 100, 55),
            frame(14, 94, 65), damage(15, 65, -35, ASSIST),
            {"type": "ko", "tick": 15, "round": 1, "target": P2, "source": None},
        )
        self.assertEqual(report["format"], "bvn-arena-report/1")
        self.assertEqual(report["attempt"]["seriesRound"], 3)
        self.assertEqual(report["attempt"]["map"], "gense")
        p1, p2 = report["players"]
        self.assertEqual((p1["kind"], p2["kind"]), ("entrant", "cpu"))
        self.assertEqual((p1["roster"], p1["assist"], p2["roster"]), (["ichigo"], "rukia", ["naruto"]))
        self.assertEqual(p1["damage"]["hpLostUnattributed"], 6)
        self.assertEqual(p1["damage"]["hpDealtConfirmed"], 110)
        self.assertEqual((p2["damage"]["hpLost"], p2["damage"]["hpHealed"]), (110, 10))
        move, = report["rounds"][0]["moves"]
        self.assertEqual((move["hits"], move["hpLost"]), (3, 110))
        self.assertEqual(move["source"], ASSIST)
        ko, = [event for event in report["events"] if event["type"] == "ko"]
        self.assertEqual(ko["source"], ASSIST)
        hits = [event for event in report["events"] if event["type"] == "damage"]
        self.assertEqual([event["amount"] for event in hits], [27, 18, 6, 65])
        self.assertIn("露琪亚", battle_report.render_report(report))

    def test_snapshot_uses_native_endpoints_without_assigning_unseen_damage(self):
        report = self.export(
            frame(10, 100, 100), damage(11, 90, 75, ASSIST), frame(12, 100, 70),
            damage(13, 70, 55, dict(ASSIST, confirmed=False)), frame(14, 100, 55),
            damage(15, 100, 90, {"kind": "self", "seat": 1, "confirmed": True}, target=P1),
        )
        p1, p2 = [player["damage"] for player in report["players"]]
        self.assertEqual((p2["hpLost"], p2["hpLostUnattributed"]), (45, 30))
        self.assertEqual((p1["hpLostToSelfConfirmed"], p1["hpDealtConfirmed"]), (10, 15))

    def test_legacy_snapshots_do_not_count_refills_or_guess_attacker(self):
        report = self.export(
            frame(10, 100, 100), {"type": "applied", "tick": 11, "p1": ["attack"]},
            frame(12, 100, 70), frame(13, 100, 0),
            frame(14, 100, 100, p2=fighter(2, "sasuke", "佐助")),
            frame(16, 100, 92, p2=fighter(2, "sasuke", "佐助")),
            frame(20, 100, 100, round=2, p2=fighter(2, "sasuke", "佐助")),
            frame(22, 100, 10, round=2, p2=fighter(2, "sasuke", "佐助")),
            damage(22, 100, 10, ASSIST, round=2) | {"matchId": "other-match"},
            interrupted="测试中断",
        )
        self.assertEqual(report["attempt"]["status"], "interrupted")
        self.assertEqual(report["attempt"]["result"]["reason"], "测试中断")
        p1, p2 = [player["damage"] for player in report["players"]]
        self.assertEqual(p1["hpDealtConfirmed"], 0)
        self.assertEqual((p2["hpLost"], p2["hpLostUnattributed"], p2["hpHealed"]), (198, 198, 0))
        self.assertEqual([round["round"] for round in report["rounds"]], [1, 2])
        self.assertTrue(all(event["source"] is None for event in report["events"]))
        self.assertIn("来源未确认", battle_report.render_report(report))

    def test_forfeit_with_only_draft_is_still_exportable(self):
        report = self.export(
            {"type": "draft", "p1": {"roster": ["ichigo"], "assist": "rukia"}, "p2": {},
             "errors": [None, "选角超时"]},
            result={"winner": 1, "reason": "forfeit", "detail": "P2 选角超时"},
        )
        self.assertEqual(report["attempt"]["status"], "forfeit")
        self.assertEqual(report["attempt"]["result"]["winner"], 1)
        self.assertEqual(report["rounds"], [])
        self.assertEqual(report["players"][0]["roster"], ["ichigo"])
        self.assertIn("弃权", battle_report.render_report(report))

    def test_running_archive_cannot_overwrite_report(self):
        with self.assertRaisesRegex(ValueError, "录制"):
            battle_report.write_report(self.recording.path)
        self.assertFalse(self.recording.path.with_suffix(".bvnreport.json").exists())

    def test_human_seat_comes_from_setup_and_mode_not_the_player_name(self):
        metadata = {"practice": True, "mode": "human", "teams": [{"name": "我的队"}, {"name": "蓝队"}]}
        draft = {"type": "draft", "p1": {"human": True, "roster": ["ichigo"]},
                 "p2": {"roster": ["naruto"]}}
        report = battle_report.build_report({"status": "finished", "metadata": metadata}, [(0, draft)], "game.bvnreplay")
        self.assertEqual([player["kind"] for player in report["players"]], ["human", "entrant"])
        self.assertEqual(report["attempt"]["mode"], "human")
        self.assertIn("玩家对 AI", battle_report.render_report(report))
