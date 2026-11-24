import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import referee_api


ROOT = Path(__file__).resolve().parents[1]


class RefereeApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="BVN referee ")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.default_game = self.folder / "默认游戏"
        self.run_request = {
            "op": "run",
            "gameDir": "游戏",
            "teamA": "选手 A/entrant.json",
            "teamB": None,
            "map": "gense",
            "jobDir": "作业/1",
            "dataDir": "战报",
            "skipCutIn": True,
        }

    def request(self, name, body):
        source = self.folder / "请求" / f"{name}.json"
        source.parent.mkdir(exist_ok=True)
        source.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8-sig")
        return source, self.folder / "回复" / f"{name}-response.json"

    def call(self, name, body):
        source, response = self.request(name, body)
        code = referee_api.handle_request(source, response, default_game=self.default_game)
        return code, json.loads(response.read_text(encoding="utf-8"))

    def test_list_maps_checks_installation_and_returns_ids_and_labels(self):
        with patch.object(referee_api, "check_game", return_value=self.default_game) as check, \
             patch.object(referee_api, "list_maps", return_value=[("gense", "现世")]) as maps:
            code, reply = self.call("maps", {"op": "list_maps"})
        check.assert_called_once_with(self.default_game)
        maps.assert_called_once_with(self.default_game)
        self.assertEqual(code, 0)
        self.assertEqual(reply, {"op": "list_maps", "ok": True, "gameDir": str(self.default_game),
                                 "maps": [{"id": "gense", "name": "现世"}]})

    def test_run_resolves_paths_preserves_result_and_rejects_job_reuse(self):
        result = {"seriesId": "123", "score": [1, 0], "practice": True, "winner": "选手",
                  "recordings": ["record.bvnreplay"], "ledger": None}

        def series(game, team_a, team_b, map_id, data, report, stop, *, skip_cut_in):
            base = self.folder / "请求"
            self.assertEqual((game, team_a, data),
                             (base / "游戏", base / "选手 A/entrant.json", base / "战报"))
            self.assertIsNone(team_b)
            self.assertEqual(map_id, "gense")
            self.assertTrue(skip_cut_in)
            self.assertFalse(stop.is_set())
            report("开始练习")
            return result

        with patch.object(referee_api, "run_series", side_effect=series) as runner:
            code, reply = self.call("run", self.run_request)
            self.assertEqual(code, 0)
            self.assertEqual(reply["state"], "completed")
            self.assertEqual(reply["result"], result)
            status_path = Path(reply["jobDir"]) / "status.json"
            original = status_path.read_bytes()
            code, duplicate = self.call("duplicate", self.run_request)
            self.assertEqual(code, 1)
            self.assertEqual(duplicate["error"]["code"], "job_exists")
            self.assertEqual(status_path.read_bytes(), original)
            runner.assert_called_once()

        code, status = self.call("status", {"op": "status", "jobDir": "作业/1"})
        self.assertEqual(code, 0)
        self.assertEqual(status["result"], result)
        code, stop = self.call("stop", {"op": "stop", "jobDir": "作业/1"})
        self.assertEqual(code, 0)
        self.assertEqual(stop["state"], "completed")
        self.assertFalse(stop["stopRequested"])

    def test_status_and_stop_while_a_runner_is_active(self):
        started = threading.Event()
        source, response = self.request("run", self.run_request)
        codes = []

        def series(game, team_a, team_b, map_id, data, report, stop, *, skip_cut_in):
            report("正在等待游戏")
            started.set()
            if not stop.wait(3):
                raise RuntimeError("没有收到停止请求")
            raise InterruptedError("主持人停止了比赛")

        with patch.object(referee_api, "run_series", side_effect=series):
            worker = threading.Thread(target=lambda: codes.append(
                referee_api.handle_request(source, response, default_game=self.default_game)))
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                code, progress = self.call("status", {"op": "status", "jobDir": "作业/1"})
                self.assertEqual(code, 0)
                self.assertEqual(progress["state"], "running")
                self.assertEqual(progress["message"], "正在等待游戏")
                self.assertFalse(progress["stopRequested"])
                self.assertFalse(response.exists())
                code, stopping = self.call("stop", {"op": "stop", "jobDir": "作业/1"})
                self.assertEqual(code, 0)
                self.assertTrue(stopping["stopRequested"])
            finally:
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(codes, [2])
        reply = json.loads(response.read_text(encoding="utf-8"))
        self.assertEqual(reply["state"], "stopped")
        self.assertTrue(reply["stopRequested"])
        self.assertEqual(reply["error"]["code"], "stopped")

    def test_failure_persists_and_default_data_path_is_in_the_game(self):
        request = {k: v for k, v in self.run_request.items()
                   if k not in {"gameDir", "dataDir", "skipCutIn"}}
        with patch.object(referee_api, "run_series", side_effect=RuntimeError("比赛暂停")) as runner:
            code, reply = self.call("run", request)
        args = runner.call_args.args
        self.assertEqual(args[0], self.default_game)
        self.assertEqual(args[4], self.default_game / "AIArena/data")
        self.assertFalse(runner.call_args.kwargs["skip_cut_in"])
        self.assertEqual(code, 1)
        self.assertEqual(reply["state"], "failed")
        self.assertEqual(reply["error"]["message"], "比赛暂停")
        code, reply = self.call("status", {"op": "status", "jobDir": "作业/1"})
        self.assertEqual(code, 0)
        self.assertEqual(reply["state"], "failed")
        self.assertEqual(reply["error"]["code"], "run_failed")

    def test_invalid_requests_receive_structured_errors(self):
        with patch.object(referee_api, "run_series") as runner:
            for name, request in [
                ("teamB", {k: v for k, v in self.run_request.items() if k != "teamB"}),
                ("skipCutIn", dict(self.run_request, skipCutIn="false")),
                ("operation", {"op": "unknown"}),
                ("object", []),
            ]:
                with self.subTest(name=name):
                    code, reply = self.call(name, request)
                    self.assertEqual(code, 1)
                    self.assertFalse(reply["ok"])
                    self.assertEqual(reply["error"]["code"], "invalid_request")
            runner.assert_not_called()
        code, missing = self.call("status", {"op": "status", "jobDir": "作业/9"})
        self.assertEqual(code, 1)
        self.assertEqual(missing["error"]["code"], "job_not_found")

    def test_cli_writes_a_json_error_for_malformed_json_without_opening_ui(self):
        source, response = self.request("bad", {})
        source.write_text("?", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(ROOT / "arena_app.py"), "--referee-api",
                               str(source), str(response)], capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        reply = json.loads(response.read_text(encoding="utf-8"))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "invalid_request")

    def test_response_protects_request_replays_and_status_snapshots(self):
        source, response = self.request("maps", {"op": "list_maps"})
        replay = self.folder / "match.bvnreplay"
        status = self.folder / "status.json"
        replay.write_bytes(b"existing recording")
        status.write_bytes(b'{"state":"running"}')
        with patch.object(referee_api, "check_game", return_value=self.default_game), \
             patch.object(referee_api, "list_maps", return_value=[]):
            for protected in (source, replay, status):
                with self.subTest(protected=protected.name):
                    original = protected.read_bytes()
                    code = referee_api.handle_request(source, protected, default_game=self.default_game)
                    self.assertEqual(code, 1)
                    self.assertEqual(protected.read_bytes(), original)
            for _ in range(2):
                self.assertEqual(
                    referee_api.handle_request(source, response, default_game=self.default_game), 0)
        self.assertTrue(json.loads(response.read_text(encoding="utf-8"))["ok"])
