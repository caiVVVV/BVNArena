"""Localhost transport and BO7 series administration for BVN Sports 3.8.8.5."""
import contextlib
import ctypes
import json
import os
import secrets
import select
import socket
import struct
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import entrants
from installation import PREFERRED_ASSIST, check_game, default_roster, installed_choices, selection_names


PORT = 17638
ACTION_SECONDS = .040
UNLIMITED_ROUND_TIME = -1
DRAFT_SECONDS = 15.0
GAME_START_SECONDS = 50.0
FRAME_IDLE_SECONDS = 35.0
MAX_LINE = 512 * 1024


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def verify_game(game_dir):
    root = check_game(game_dir)
    if not (root / "assets" / "plugin" / "ArenaBridge.swf").is_file():
        raise ValueError("比赛副本缺少 assets/plugin/ArenaBridge.swf，请先安装桥接插件")
    return root / "Bleach vs. Naruto.exe"


def _check_stop(stop):
    if stop.is_set():
        raise InterruptedError("主持人停止了比赛")


def _client_pid(client_port, server_port):
    """Read the peer endpoint's owner from the Windows IPv4 TCP table."""
    if os.name != "nt":
        raise RuntimeError("此游戏裁判需要 Windows")
    size = ctypes.c_ulong(0)
    get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value) if size.value else None
        code = get_table(buf, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if code == 122:  # ERROR_INSUFFICIENT_BUFFER
            continue
        if code or buf is None:
            raise OSError(code, "无法查询 TCP 连接所属进程")
        data = buf.raw
        count, = struct.unpack_from("<I", data)
        local_address, = struct.unpack("<I", socket.inet_aton("127.0.0.1"))
        for index in range(count):
            _, local_ip, local, remote_ip, remote, pid = struct.unpack_from("<6I", data, 4+24*index)
            if (local_ip == local_address and remote_ip == local_address and
                    socket.ntohs(local & 65535) == client_port and
                    socket.ntohs(remote & 65535) == server_port):
                return pid
        return None
    raise OSError("TCP 连接表持续变化，无法验证游戏进程")


class Bridge:
    def __init__(self, connection, proc, stop):
        self.socket = connection
        self.proc = proc
        self.pid = proc.pid
        self.stop = stop
        self.buffer = bytearray()
        self.catalog = None
        connection.settimeout(.20)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, msg):
        try:
            self.socket.sendall(json.dumps(msg, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
        except OSError as exc:
            raise ConnectionError("与游戏的写入连接已关闭") from exc

    def has_newer_frame(self, match_id, tick):
        # AIR runs in real time. When a bot repeatedly times out, skip decisions
        # already superseded on the wire while preserving their replay events.
        if len(self.buffer) < MAX_LINE and select.select([self.socket], [], [], 0)[0]:
            try:
                self.buffer.extend(self.socket.recv(65536))
            except OSError as exc:
                raise ConnectionError("游戏桥接连接已断开") from exc
        for line in self.buffer.split(b"\n")[:-1]:
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(msg, dict) and msg.get("matchId") == match_id:
                if msg.get("type") == "result":
                    return True
                if msg.get("type") == "frame" and type(msg.get("tick")) is int and msg["tick"] > tick:
                    return True
        return False

    def receive(self, deadline):
        while True:
            _check_stop(self.stop)
            if time.monotonic() >= deadline:
                raise TimeoutError("等待游戏桥接消息超时")
            index = self.buffer.find(b"\n")
            if index >= 0:
                packet = bytes(self.buffer[:index])
                del self.buffer[:index+1]
                try:
                    msg = json.loads(packet.decode("utf-8"))
                    if index > MAX_LINE or not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                        raise ValueError("数据格式错误")
                    return msg
                except (ValueError, RecursionError) as exc:
                    raise ConnectionError("游戏桥接发来无效 JSON 消息") from exc
            if len(self.buffer) > MAX_LINE:
                raise ConnectionError("游戏桥接消息过长")
            if self.proc.poll() is not None:
                raise ConnectionError("本场游戏进程已退出")
            try:
                chunk = self.socket.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                raise ConnectionError("游戏桥接连接已断开") from exc
            if not chunk:
                raise ConnectionError("游戏桥接已关闭连接")
            self.buffer.extend(chunk)


def _catalog(msg):
    for key in ("fighters", "assists", "maps"):
        items = msg.get(key)
        if not isinstance(items, list) or not items or any(type(s) is not str or not s for s in items):
            raise ConnectionError(f"游戏未提供有效 {key} 名单")
    if type(msg.get("teamSize")) is not int or not 1 <= msg["teamSize"] <= len(set(msg["fighters"])):
        raise ConnectionError("游戏队伍人数和角色名单不一致")
    return {key: msg[key] for key in ("fighters", "assists", "maps", "teamSize")}


def _installed_catalog(game_dir, catalog):
    assets = game_dir / "assets"
    fighters, assists = installed_choices(game_dir)
    installed = {
        "fighters": fighters,
        "assists": assists,
        "maps": {p.stem for p in (assets / "map").glob("*.swf")},
    }
    return _catalog({**catalog, **{k:[v for v in catalog[k] if v in ids] for k, ids in installed.items()}})


@contextlib.contextmanager
def _open_game(game_dir, stop, status):
    _check_stop(stop)
    exe = verify_game(game_dir)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise RuntimeError(f"本地端口 {PORT} 不可用，请关闭正在主持的另一场比赛后重试") from exc
        listener.listen(4)
        listener.settimeout(.20)
        status("裁判已监听，正在启动游戏并等待插件")
        proc = subprocess.Popen([str(exe)], cwd=game_dir)
        try:
            deadline = time.monotonic() + GAME_START_SECONDS
            while time.monotonic() < deadline:
                _check_stop(stop)
                if proc.poll() is not None:
                    raise ConnectionError(f"游戏在桥接握手前退出（退出码 {proc.returncode}）")
                try:
                    client, peer = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    if _client_pid(peer[1], PORT) != proc.pid:
                        continue
                    bridge = Bridge(client, proc, stop)
                    hello = bridge.receive(deadline)
                    if hello.get("type") != "hello" or hello.get("protocol") != 1:
                        raise ConnectionError("游戏桥接协议版本不兼容")
                    while True:
                        msg = bridge.receive(deadline)
                        if msg["type"] == "catalog":
                            bridge.catalog = _installed_catalog(Path(game_dir), _catalog(msg))
                            yield bridge
                            return
                        if msg["type"] == "status":
                            status(str(msg.get("message", ""))[:500])
            raise TimeoutError("游戏未连接裁判，请检查赛场副本和插件是否已启用")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


@contextlib.contextmanager
def _series_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".host.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("本地比赛档案已有裁判正在使用") from exc
        yield


def _load_series(data_dir, teams, requested_map, skip_cut_in, human_pick=None):
    practice = teams[1] is None or human_pick is not None
    signature = [*(team.fingerprint if team else None for team in teams), requested_map,
                 {"fightTime":UNLIMITED_ROUND_TIME, "skipCutIn":skip_cut_in}]
    folder = data_dir / "series"
    if not practice:
        for path in sorted(folder.glob("*.json"), reverse=True):
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["signature"] == signature and not state["complete"]:
                state.setdefault("mode", "ranked")
                return state, path
    sid = uuid.uuid4().hex
    state = {"seriesId": sid, "signature":signature, "createdAt":_utc(), "map":requested_map,
             "registration":[{"name":t.name,"color":t.color} if t else
                             {"name":"游戏 CPU","color":"#808080"} for t in teams],
             "startSeat":0 if practice else secrets.randbelow(2), "score":[0,0],
             "practice":practice, "mode":"cpu" if practice else "ranked",
             "skipCutIn":skip_cut_in, "fightTime":UNLIMITED_ROUND_TIME,
             "complete":False, "attempts":[]}
    if human_pick is not None:
        state.update(mode="human", humanPick=human_pick)
        state["registration"][0] = {"name":"玩家", "color":"#08786E"}
    path = None if practice else folder / (time.strftime("%Y%m%dT%H%M%S") + "-" + sid + ".json")
    return state, path


def _new_recording(data_dir, metadata):
    from replay import ArenaRecording
    return ArenaRecording(data_dir, metadata)


def _fingerprint(game_dir):
    from native_replay import resource_fingerprint
    return resource_fingerprint(game_dir)


def _demo(path):
    from native_replay import write_demo
    return write_demo(path)


def _pick(reply, catalog):
    if not isinstance(reply, dict):
        raise ValueError(str(reply or "选角超时"))
    roster, assist = reply.get("roster"), reply.get("assist")
    if not isinstance(roster, list) or len(roster) != catalog["teamSize"]:
        raise ValueError("选角人数不符")
    if any(type(item) is not str or item not in catalog["fighters"] for item in roster):
        raise ValueError("角色不在游戏名单内")
    if len(set(roster)) != len(roster):
        raise ValueError("同队不能重复选角")
    if assist not in catalog["assists"]:
        raise ValueError("援助不在游戏名单内")
    return {"roster":roster, "assist":assist}


def _forfeit(failures):
    if all(failures):
        raise RuntimeError(f"双方参赛程序同时失败，本局未判分。P1 {failures[0]} / P2 {failures[1]}")
    if any(failures):
        failed = 0 if failures[0] else 1
        return 2 - failed, "forfeit", f"P{failed+1} {failures[failed]}"
    return None


def _play(game, teams, sides, state, match_id, rec, stop, status):
    catalog = game.catalog
    desc = [dict(state["registration"][idx]) for idx in sides]
    human = state.get("humanPick")
    if human is not None:
        roster, assist = human["roster"], human["assist"]
        if roster is None:
            roster = default_roster(catalog["fighters"], catalog["teamSize"])
        if assist is None:
            assist = PREFERRED_ASSIST if PREFERRED_ASSIST in catalog["assists"] else catalog["assists"][0]
        human = dict(_pick({"roster":roster, "assist":assist}, catalog), human=True)
    failures = [None, None]
    with contextlib.ExitStack() as stack:
        bots = []
        for seat, idx in enumerate(sides):
            runner = None
            if teams[idx] is not None:
                try:
                    runner = entrants.BotProcess(teams[idx])
                    stack.callback(runner.close)
                except OSError as exc:
                    failures[seat] = str(exc)
            bots.append(runner)
        score = [state["score"][idx] for idx in sides]
        common = {"matchId":match_id, "map":state["map"], "score":score, "teams":desc}
        drafts = [dict(type="draft", seat=i+1, **common, **catalog) for i in range(2)]
        started = time.monotonic()
        replies = entrants.exchange(bots, drafts, DRAFT_SECONDS, stop)
        picks = [{}, {}]
        for i, reply in enumerate(replies):
            if teams[sides[i]] is None:
                picks[i] = human if human is not None else {
                    "roster":default_roster(catalog["fighters"], catalog["teamSize"]),
                    "assist":catalog["assists"][0], "practiceCPU":True}
            elif not failures[i]:
                try:
                    picks[i] = _pick(reply, catalog)
                except ValueError as exc:
                    failures[i] = str(exc)
        rec.append({"type":"draft", "matchId":match_id, "elapsedMs":round(1000*(time.monotonic()-started)),
                    "deadlineMs":int(1000*DRAFT_SECONDS), "actionDeadlineMs":int(1000*ACTION_SECONDS),
                    "p1":picks[0], "p2":picks[1], "errors":failures})
        decision = _forfeit(failures)
        if decision:
            return decision
        fighter_names, assist_names = game.selection_names
        public_lineups = [{"roster":pick["roster"], "assist":pick["assist"],
                           "rosterNames":[fighter_names.get(i, i) for i in pick["roster"]],
                           "assistName":assist_names.get(pick["assist"], pick["assist"])} for pick in picks]
        lineups = [{**side, **pick, "colorValue":int(side["color"][1:], 16)}
                   for side, pick in zip(desc, picks)]
        setup = {"type":"setup", "matchId":match_id, "map":state["map"], "score":score,
                 "p1":lineups[0], "p2":lineups[1], "skipCutIn":state.get("skipCutIn",False),
                 "fightTime":state["fightTime"]}
        rec.append(setup)
        game.send(setup)
        status(f"第 {sum(score)+1} 局  P1 {desc[0]['name']}  {score[0]} ∶ {score[1]}  P2 {desc[1]['name']}")
        previous = -1
        idle_deadline = time.monotonic() + GAME_START_SECONDS
        while True:
            msg = game.receive(idle_deadline)
            event = msg["type"]
            if event == "status":
                status(str(msg.get("message", ""))[:500])
                continue
            if msg.get("matchId") != match_id:
                continue
            if event == "result":
                winner = msg.get("winner")
                if type(winner) is not int or winner not in (0, 1, 2):
                    raise ConnectionError("游戏返回了未知胜方，需重赛")
                if previous < 0:
                    raise ConnectionError("未收到交战帧便收到结果，需重赛")
                rec.append(msg)
                return winner, "team_elimination" if winner else "draw", ""
            if event in ("applied", "cutInSkipped", "attack", "damage", "ko"):
                rec.append(msg)
                continue
            tick = msg.get("tick")
            if event != "frame" or type(tick) is not int or tick <= previous:
                continue
            players = msg.get("players")
            if not isinstance(players, list) or len(players) != 2 or not all(isinstance(p,dict) for p in players):
                raise ConnectionError("游戏没有提供两方角色状态")
            previous = tick
            idle_deadline = time.monotonic() + FRAME_IDLE_SECONDS
            rec.append(msg)
            if game.has_newer_frame(match_id, tick):
                continue
            opponents = [dict(lineup, activeId=player.get("id"),
                              activeName=player.get("name") or fighter_names.get(player.get("id")))
                         for lineup, player in zip(public_lineups, players)]
            frames = [{**msg, "seat":i+1, "lineups":public_lineups, "opponent":opponents[1-i]}
                      for i in range(2)]
            replies = entrants.exchange(bots, frames, ACTION_SECONDS, stop)
            actions = [[], []]
            for i, reply in enumerate(replies):
                if isinstance(reply, entrants.BotFailure):
                    failures[i] = str(reply)
                elif isinstance(reply, dict):
                    try:
                        actions[i] = entrants.buttons(reply.get("buttons"))
                    except ValueError:
                        pass  # Bad input has the same effect as a missed frame.
            decision = _forfeit(failures)
            if decision:
                return decision
            command = {"type":"input", "matchId":match_id, "tick":tick, "p2":actions[1]}
            if human is None:
                command["p1"] = actions[0]
            game.send(command)


def _attempt(game, teams, state, path, data_dir, game_dir, stop, status):
    a_seat = (state["startSeat"] + sum(state["score"])) % 2
    sides = [a_seat, 1-a_seat]
    match_id = uuid.uuid4().hex
    info = {"matchId":match_id, "seriesId":state["seriesId"], "gameDir":str(game_dir),
            "gameVersion":"3.8.8.5", "resourceFingerprint":state["resourceFingerprint"],
            "map":state["map"], "practice":state["practice"], "mode":state["mode"],
            "skipCutIn":state.get("skipCutIn",False),
            "fightTime":state["fightTime"],
            "round":sum(state["score"])+1,
            "teams":[dict(state["registration"][i], side="AB"[i]) for i in sides],
            "seriesScore":list(state["score"]), "createdAt":_utc()}
    rec = _new_recording(data_dir, info)
    attempt = {"matchId": match_id, "round":info["round"], "seats":sides,
               "recording":str(rec.path), "startedAt":info["createdAt"], "status":"playing"}
    state["attempts"].append(attempt)
    try:
        rec.bind_game(game.pid)
        if path:
            _atomic_json(path, state)
        winner, reason, detail = _play(game, teams, sides, state, match_id, rec, stop, status)
        new_score = list(state["score"])
        if winner:
            new_score[sides[winner-1]] += 1
        result = {"winner":winner, "winnerName":info["teams"][winner-1]["name"] if winner else None,
                  "score":new_score, "reason":reason, "detail":detail,
                  "round":info["round"], "finishedAt":_utc()}
        # Commit the authoritative decision and score in one replace. Losing power
        # while finalizing video cannot produce a second award for this decision.
        attempt.update(result, status="finished")
        state["score"] = new_score
        state["complete"] = state["practice"] or max(new_score) >= 4
        if path:
            _atomic_json(path, state)
        rec.finish(result)
        _export_report(rec.path, status)
        if reason in ("team_elimination", "draw") and Path(rec.path).is_file():
            try:
                compact_path = _demo(rec.path)
                status(f"已导出游戏输入回放 {Path(compact_path).name}")
            except Exception as exc:
                status(f"完整录像已保存，小型输入回放导出失败。{exc}")
        verdict = result["winnerName"]+" 获胜" if winner else (
            "平局" if state["practice"] else "平局，原座位重赛")
        status(f"{verdict}  "
               f"{new_score[0]} ∶ {new_score[1]}" + (f"  {detail}" if detail else ""))
    except BaseException as exc:
        if attempt["status"] == "playing":
            attempt.update(status="interrupted", detail=str(exc), finishedAt=_utc())
            if path:
                _atomic_json(path, state)
        rec.interrupt(str(exc))
        _export_report(rec.path, status)
        raise
    return result


def _export_report(path, status):
    if Path(path).is_file():
        from battle_report import write_report
        try:
            report = write_report(path)
            status(f"已导出赛后复盘 {report.name}")
        except Exception as exc:
            status(f"比赛录像已保存，赛后复盘导出失败。{exc}")


def run_series(game_dir, team_a, team_b, map_id, data_dir, on_status, stop, *, skip_cut_in=False):
    """Run a ranked first-to-four series or one unranked CPU practice game."""
    return _run_matches(game_dir, (team_a, team_b), map_id, data_dir, on_status, stop, skip_cut_in=skip_cut_in)


def run_human_match(game_dir, opponent, map_id, data_dir, on_status, stop, *,
                    roster=None, assist=None, skip_cut_in=False):
    """Run one unranked 3v3 game with the player on P1 and an entrant on P2."""
    if opponent is None:
        raise ValueError("请选择 P2 的参赛选手")
    human_pick = {"roster":list(roster) if isinstance(roster, list) else roster, "assist":assist}
    return _run_matches(game_dir, (None, opponent), map_id, data_dir, on_status, stop,
                        skip_cut_in=skip_cut_in, human_pick=human_pick)


def _run_matches(game_dir, team_paths, map_id, data_dir, on_status, stop, *, skip_cut_in, human_pick=None):
    game_dir, data_dir = Path(game_dir).resolve(), Path(data_dir).resolve()
    verify_game(game_dir)
    teams = [entrants.load_team(path) if path is not None else None for path in team_paths]
    with _series_lock(data_dir):
        state, path = _load_series(data_dir, teams, map_id, skip_cut_in, human_pick)
        labels = selection_names(game_dir)
        fingerprint = _fingerprint(game_dir)
        previous = state.get("resourceFingerprint", fingerprint)
        if previous != fingerprint:
            from native_replay import matches_resource_fingerprint
            if not matches_resource_fingerprint(game_dir, previous, current=fingerprint):
                raise ValueError("当前游戏资源与已有系列赛不同。请恢复原比赛环境，或使用新的比赛数据目录。")
        state["resourceFingerprint"] = fingerprint
        for attempt in state["attempts"]:
            if attempt["status"] == "playing":
                attempt.update(status="interrupted", detail="裁判在上次比赛中退出", finishedAt=_utc())
        if path:
            _atomic_json(path, state)
        retries = draws = 0
        while not state["complete"]:
            _check_stop(stop)
            try:
                with _open_game(game_dir, stop, on_status) as game:
                    catalog = game.catalog = _catalog(game.catalog)
                    game.selection_names = labels
                    _atomic_json(data_dir/"catalog.json", catalog)
                    if state["map"] in ("", "random", None):
                        state["map"] = secrets.choice(catalog["maps"])
                    if state["map"] not in catalog["maps"]:
                        raise ValueError(f"游戏地图名单中没有 {state['map']}")
                    while not state["complete"]:
                        _check_stop(stop)
                        result = _attempt(game, teams, state, path, data_dir, game_dir, stop, on_status)
                        retries = 0
                        draws = draws+1 if result["winner"] == 0 else 0
                        if not state["complete"]:
                            game.send({"type":"reset", "matchId":state["attempts"][-1]["matchId"]})
                            if draws >= 3:
                                raise RuntimeError("连续三次平局，比赛暂停。再次开始可从当前比分续赛。")
            except (ConnectionError, TimeoutError) as exc:
                retries += 1
                if retries > 2:
                    raise ConnectionError(f"游戏连接连续失败三次，已保存当前比分。{exc}") from exc
                on_status(f"游戏连接中断，保留比分并自动重连（{retries}/2）。{exc}")
        from battle_report import SUFFIX
        recordings = [a["recording"] for a in state["attempts"]]
        reports = [Path(recording).with_suffix(SUFFIX) for recording in recordings]
        return {"seriesId":state["seriesId"], "score":state["score"], "practice":state["practice"],
                "winner": result["winnerName"], "recordings":recordings,
                "reports":[str(report) for report in reports if report.is_file()],
                "ledger":str(path) if path else None}
