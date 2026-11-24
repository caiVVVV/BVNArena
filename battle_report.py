"""Summarize the game evidence stored in one finalized arena recording."""

import json
import math
import os
from pathlib import Path
import tempfile


FORMAT = "bvn-arena-report/1"
SUFFIX = ".bvnreport.json"
SOURCE_LABELS = {"fighter": "主战角色", "assist": "援助", "projectile": "飞行道具", "self": "自身"}


def _object(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) and value else None


def _seat(value):
    return value if type(value) is int and value in (1, 2) else None


def _hp(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return max(0, value)
    return None


def _fighter(value, seat=None):
    value = _object(value)
    seat = seat or _seat(value.get("seat"))
    fighter_id = _text(value.get("id"))
    if seat is not None and fighter_id is not None:
        return {"seat": seat, "id": fighter_id, "name": _text(value.get("name"))}
    return None


def _source(value):
    if not isinstance(value, dict):
        return None
    kind = _text(value.get("kind"))
    return {"kind": kind if kind in SOURCE_LABELS else "unknown",
            "seat": _seat(value.get("seat")) or 0,
            "id": _text(value.get("id")), "name": _text(value.get("name")),
            "move": _text(value.get("move")),
            "confirmed": kind in SOURCE_LABELS and value.get("confirmed") is True}


def _damage_summary():
    return {"hpLost": 0, "hpHealed": 0, "hpDealtConfirmed": 0, "hpLostUnattributed": 0,
            "hpLostToOpponentConfirmed": 0, "hpLostToSelfConfirmed": 0}


def _count_hp(event, players):
    damage = players[event["target"]["seat"] - 1]["damage"]
    amount = event["amount"]
    if event["type"] == "heal":
        damage["hpHealed"] += amount
        return
    damage["hpLost"] += amount
    source = _object(event["source"])
    seat = _seat(source.get("seat")) if source.get("confirmed") else None
    if seat is None:
        damage["hpLostUnattributed"] += amount
    elif seat == event["target"]["seat"]:
        damage["hpLostToSelfConfirmed"] += amount
    elif source["kind"] != "self":
        damage["hpLostToOpponentConfirmed"] += amount
        players[seat - 1]["damage"]["hpDealtConfirmed"] += amount
    else:
        damage["hpLostUnattributed"] += amount


class BattleReport:
    def __init__(self):
        self.events = []
        self.rounds = {}
        self.previous_hp = {}
        self.knockouts = {}
        self.coverage = {"frames": 0, "runtimeHpChanges": 0, "observedHpChanges": 0,
                         "runtimeAttackEvents": 0, "runtimeKnockoutEvents": 0}

    def _round(self, round_number, tick):
        if round_number not in self.rounds:
            self.rounds[round_number] = {
                "round": round_number, "startTick": tick, "endTick": tick,
                "players": [{"seat": seat, "fighters": {}, "damage": _damage_summary()}
                            for seat in (1, 2)], "moves": {},
            }
        entry = self.rounds[round_number]
        entry["startTick"] = min(tick, entry["startTick"])
        entry["endTick"] = max(tick, entry["endTick"])
        return entry

    def _remember_fighter(self, fighter, round_number, tick):
        player = self._round(round_number, tick)["players"][fighter["seat"] - 1]
        known = player["fighters"].get(fighter["id"])
        if known is None or (fighter["name"] and not known["name"]):
            player["fighters"][fighter["id"]] = fighter

    def _hp_change(self, tick, round_number, target, before, after, source, evidence):
        difference = before - after
        if difference == 0:
            return
        event = {"type": "damage" if difference > 0 else "heal",
                 "tick": tick, "round": round_number, "target": target,
                 "hpBefore": before, "hpAfter": after, "amount": abs(difference),
                 "source": source, "evidence": evidence}
        self.events.append(event)
        counter = "runtimeHpChanges" if evidence == "runtime" else "observedHpChanges"
        self.coverage[counter] += 1
        if difference > 0 and after == 0:
            self._knockout(tick, round_number, target, source, "hp_zero")

    def _observe_hp(self, tick, round_number, target, hp):
        self._remember_fighter(target, round_number, tick)
        seat = target["seat"]
        identity = (round_number, target["id"])
        previous = self.previous_hp.get(seat)
        if previous is not None and previous[:2] == identity and previous[2] <= tick:
            self._hp_change(tick, round_number, target, previous[3], hp, None, "hp_observation")
        self.previous_hp[seat] = (*identity, tick, hp)

    def _knockout(self, tick, round_number, target, source, evidence):
        self._remember_fighter(target, round_number, tick)
        key = (round_number, target["seat"], target["id"])
        knockout = self.knockouts.get(key)
        if knockout is None:
            knockout = {"type": "ko", "tick": tick, "round": round_number, "target": target,
                        "source": source, "evidence": evidence}
            self.knockouts[key] = knockout
            self.events.append(knockout)
        elif evidence == "runtime":
            knockout.update(tick=tick, target=target, evidence=evidence)
            if source is not None and (source["confirmed"] or not _object(knockout["source"]).get("confirmed")):
                knockout["source"] = source
        elif knockout["tick"] == tick and source is not None and source["confirmed"]:
            if not _object(knockout["source"]).get("confirmed"):
                knockout["source"] = source

    def ingest(self, event):
        tick, round_number = event.get("tick"), event.get("round")
        if type(tick) is not int or type(round_number) is not int or tick < 0 or round_number < 0:
            return
        kind = event.get("type")
        if kind == "frame":
            players = event.get("players")
            if not isinstance(players, list):
                return
            self._round(round_number, tick)
            self.coverage["frames"] += 1
            for seat, player in enumerate(players[:2], 1):
                target = _fighter(player, seat)
                hp = _hp(_object(player).get("hp"))
                if target is not None and hp is not None:
                    self._observe_hp(tick, round_number, target, hp)
        elif kind == "damage":
            target = _fighter(event.get("target"))
            before, after = _hp(event.get("hpBefore")), _hp(event.get("hpAfter"))
            if target is None or before is None or after is None:
                return
            self._observe_hp(tick, round_number, target, before)
            self._hp_change(tick, round_number, target, before, after, _source(event.get("source")), "runtime")
            self.previous_hp[target["seat"]] = (round_number, target["id"], tick, after)
        elif kind == "attack":
            seat = _seat(event.get("seat"))
            fighter = _fighter(event.get("fighter"), seat)
            if fighter is not None:
                self._remember_fighter(fighter, round_number, tick)
                self.coverage["runtimeAttackEvents"] += 1
                self.events.append({"type": "attack", "tick": tick, "round": round_number,
                                    "seat": fighter["seat"], "fighter": fighter, "move": _text(event.get("move"))})
        elif kind == "ko":
            target = _fighter(event.get("target"))
            if target is not None:
                self.coverage["runtimeKnockoutEvents"] += 1
                self._knockout(tick, round_number, target, _source(event.get("source")), "runtime")

    def summarize(self, players):
        for event in self.events:
            if event["type"] not in ("damage", "heal"):
                continue
            entry = self.rounds[event["round"]]
            _count_hp(event, players)
            _count_hp(event, entry["players"])
            source = _object(event["source"])
            if (event["type"] != "damage" or not source.get("confirmed") or not source["move"]
                    or not (source["id"] or source["name"])):
                continue
            target = event["target"]
            key = (source["seat"], source["kind"], source["id"] or source["name"], source["move"],
                   target["seat"], target["id"])
            moves = entry["moves"]
            if key not in moves:
                moves[key] = {"source": source, "target": target, "hits": 0, "hpLost": 0,
                              "firstTick": event["tick"], "lastTick": event["tick"]}
            move = moves[key]
            move["hits"] += 1
            move["hpLost"] += event["amount"]
            move["lastTick"] = event["tick"]
        rounds = [self.rounds[number] for number in sorted(self.rounds)]
        for entry in rounds:
            entry["moves"] = list(entry["moves"].values())
            for player in entry["players"]:
                player["fighters"] = list(player["fighters"].values())
        return rounds


def _players(metadata, lineup):
    teams = metadata.get("teams")
    teams = teams if isinstance(teams, list) else []
    players = []
    for seat in (1, 2):
        team = _object(teams[seat - 1]) if seat <= len(teams) else {}
        pick = _object(lineup.get(f"p{seat}"))
        name = _text(team.get("name")) or _text(pick.get("name"))
        roster = pick.get("roster")
        role = "entrant" if name else None
        if pick.get("human") is True or (metadata.get("mode") == "human" and seat == 1):
            role = "human"
        elif pick.get("practiceCPU") is True or (metadata.get("practice") is True and name == "游戏 CPU"):
            role = "cpu"
        players.append({"seat": seat, "side": _text(team.get("side")), "name": name,
                        "kind": role,
                        "roster": [item for item in roster if _text(item)] if isinstance(roster, list) else [],
                        "assist": _text(pick.get("assist")), "damage": _damage_summary()})
    return players


def build_report(info, events, replay_name):
    metadata = _object(info.get("metadata"))
    status = info.get("status")
    if status == "recording":
        raise ValueError("回放仍在录制或未写入结算，暂时无法导出赛后复盘。")
    result = _object(info.get("result"))
    if status == "finished" and result.get("reason") == "forfeit":
        status = "forfeit"
    elif status not in ("finished", "forfeit", "interrupted"):
        status = "unknown"
    match_id = metadata.get("matchId")
    report = BattleReport()
    lineup = {}
    for _, event in events:
        if not isinstance(event, dict) or (match_id and event.get("matchId") not in (None, match_id)):
            continue
        if event.get("type") in ("draft", "setup"):
            lineup = event
        else:
            report.ingest(event)
    players = _players(metadata, lineup)
    rounds = report.summarize(players)
    mode = metadata.get("mode")
    if mode is None:
        if any(player["kind"] == "human" for player in players):
            mode = "human"
        elif metadata.get("practice") is True:
            mode = "cpu"
        elif metadata.get("practice") is False:
            mode = "ranked"
    return {"format": FORMAT, "replay": replay_name,
            "attempt": {"matchId": match_id, "seriesId": metadata.get("seriesId"),
                        "seriesRound": metadata.get("round"), "map": metadata.get("map") or lineup.get("map"),
                        "practice": metadata.get("practice"), "mode": mode, "status": status, "result": result,
                        "scoreBefore": metadata.get("seriesScore"), "createdAt": metadata.get("createdAt"),
                        "durationUs": info.get("durationUs")},
            "players": players, "rounds": rounds, "events": sorted(report.events, key=lambda event: event["tick"]),
            "coverage": report.coverage}


def write_report(archive):
    from replay_viewer import ReplayReader

    archive = Path(archive)
    reader = ReplayReader(archive)
    try:
        content = build_report(reader.info, reader.events(), archive.name)
    finally:
        reader.close()
    destination = archive.with_suffix(SUFFIX)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(content, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _fighter_label(fighter):
    name = fighter.get("name") or fighter.get("id")
    seat = fighter.get("seat")
    return " ".join(part for part in (f"P{seat}" if seat else None, name) if part) or "角色未记录"


def _source_label(source):
    if not _object(source).get("confirmed"):
        return "来源未确认"
    label = _fighter_label(source)
    kind = SOURCE_LABELS.get(source["kind"], "")
    return " · ".join(part for part in (label, kind, source["move"]) if part)


def _damage_text(damage):
    return (f"掉血 {damage['hpLost']:.6g}，回血 {damage['hpHealed']:.6g}，对手掉血中确认由本方造成 "
            f"{damage['hpDealtConfirmed']:.6g}，自身掉血中来源未确认 {damage['hpLostUnattributed']:.6g}")


def render_report(report):
    attempt = report["attempt"]
    result = attempt["result"]
    status = {"finished": "已结束", "forfeit": "弃权判定", "interrupted": "中断"}.get(attempt["status"], "状态未记录")
    mode = {"human": "玩家对 AI", "cpu": "人机练习", "ranked": "正式比赛"}.get(attempt.get("mode"), "比赛")
    details = [mode, f"地图 {attempt['map'] or '未记录'}", status]
    winner = _seat(result.get("winner"))
    if winner:
        winner_name = _text(result.get("winnerName")) or report["players"][winner - 1]["name"]
        details.append(f"P{winner} {winner_name} 获胜" if winner_name else f"P{winner} 获胜")
    elif result.get("winner") == 0 and attempt["status"] == "finished":
        details.append("平局")
    lines = [" · ".join(details)]
    detail = result.get("detail") or (result.get("reason") if attempt["status"] == "interrupted" else None)
    if detail:
        lines.append(str(detail))
    lines.append("")
    for player in report["players"]:
        lines.append(f"P{player['seat']}  {player['name'] or '选手未记录'}")
        lineup = "、".join(player["roster"]) or "未记录"
        lines.append(f"阵容 {lineup}   援助 {player['assist'] or '未记录'}")
        lines.append(_damage_text(player["damage"]))
    if not report["rounds"]:
        lines.extend(("", "档案中没有交战记录。"))
    knockouts = {}
    for event in report["events"]:
        if event["type"] == "ko":
            knockouts.setdefault(event["round"], []).append(event)
    for entry in report["rounds"]:
        number = entry["round"]
        lines.extend(("", f"小回合 {number}  ·  第 {entry['startTick']} 至 {entry['endTick']} 帧"))
        for player in entry["players"]:
            names = "、".join(fighter["name"] or fighter["id"] for fighter in player["fighters"])
            lines.append(f"P{player['seat']} {names or '角色未记录'}  {_damage_text(player['damage'])}")
        for event in knockouts.get(number, ()):
            label = "被击倒" if event["evidence"] == "runtime" else "血量归零"
            lines.append(f"第 {event['tick']} 帧  {_fighter_label(event['target'])} {label}  ·  {_source_label(event['source'])}")
        for move in entry["moves"]:
            lines.append(f"{_source_label(move['source'])} → {_fighter_label(move['target'])}  "
                         f"累计 {move['hits']} 条掉血记录，共 {move['hpLost']:.6g}")
    if report["coverage"]["observedHpChanges"]:
        lines.extend(("", "部分血量变化来自相邻观测，可能漏掉观测间的事件。"))
    return "\n".join(lines)
