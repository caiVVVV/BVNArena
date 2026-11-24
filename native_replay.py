"""Compact, game-native demos with frame-by-frame verification against the bout."""
import functools
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from installation import PLUGIN, digest, read_manifest

# Earlier HUD builds with the same match protocol and game logic.
COMPATIBLE_BRIDGE_SHA256 = (
    "0ea90d5413044e34332eb0d42541b758a7c4071b47143f74363fd9c1875c5f69",
    "14860b033d02f5869619e02f01ba6a62ab7c7ea6e9ad4366eb9377b19596c4a1",
    "ed947570e917532e84fec5ab79622d214d0304411f6e371d01ec1a85246405f0",
    "634d107dc33c8687ff3b8987f12edddb2a4f36bd740fa12d160af64a0cec6c58",
)


def resource_fingerprint(game_dir, *, bridge_sha256=None):
    root = Path(game_dir).resolve()
    files = [root / "version.json", root / "Bleach vs. Naruto.exe"]
    for directory in ("assets", "Adobe AIR", "META-INF", "lang"):
        files.extend(p for p in (root / directory).rglob("*") if p.is_file()
                     and p.suffix.lower() not in {".txt", ".fla"})
    items = tuple((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in sorted(files))
    return _fingerprint(str(root), items, bridge_sha256)


@functools.lru_cache(maxsize=5)
def _fingerprint(root, items, bridge_sha256):
    composite = hashlib.sha256()
    for name, _, _ in items:
        composite.update(name.encode("utf-8") + b"\0")
        if name == PLUGIN and bridge_sha256 is not None:
            composite.update(bytes.fromhex(bridge_sha256))
            continue
        with (Path(root) / name).open("rb") as file:
            composite.update(hashlib.file_digest(file, "sha256").digest())
    return composite.hexdigest()


def matches_resource_fingerprint(game_dir, expected, *, current=None):
    if not expected:
        return False
    root = Path(game_dir).resolve()
    if (current or resource_fingerprint(root)) == expected:
        return True
    try:
        managed = read_manifest(root).get(PLUGIN)
        if managed is None or digest(root / PLUGIN) != managed:
            return False
    except (OSError, ValueError, AttributeError):
        return False
    return any(resource_fingerprint(root, bridge_sha256=previous) == expected
               for previous in COMPATIBLE_BRIDGE_SHA256)


def write_demo(archive):
    from replay_viewer import ReplayReader
    path = Path(archive)
    reader = ReplayReader(path)
    dest = path.with_suffix(".bvndemo")
    try:
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as raw:
            tmp = Path(raw.name)
        try:
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as out:
                header = {"type":"demo","format":1,"metadata":reader.info["metadata"]}
                out.write(json.dumps(header, ensure_ascii=False) + "\n")
                for _, event in reader.events():
                    if event.get("type") in {"setup", "frame", "applied", "attack", "damage", "ko", "result"}:
                        out.write(json.dumps(event, ensure_ascii=False, separators=(",",":")) + "\n")
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        reader.close()
    return dest


class MatchTape:
    def __init__(self, events):
        self.setup = None
        self.frames = {}
        self.inputs = []
        self.winner = None
        self.origin = None
        for event in events:
            kind = event.get("type")
            if kind == "setup":
                self.setup = event
            elif kind == "frame":
                tick = event["tick"]
                if self.origin is None:
                    self.origin = tick
                self.frames[tick - self.origin] = event
            elif kind == "applied" and self.origin is not None:
                self.inputs.append({"offset":event["tick"] - self.origin,
                                    "p1":event.get("keys1", event["p1"]), "p2":event.get("keys2", event["p2"])})
            elif kind == "result":
                self.winner = event.get("winner")
        if not self.setup or not self.frames or self.winner not in (0, 1, 2):
            raise ValueError("这场比赛缺少开局、交战帧或自然结算，无法校验游戏重演。")


def compare_frame(expected, actual):
    if expected.get("round") != actual.get("round") or expected.get("map") != actual.get("map"):
        return "场景或小回合不同"
    if expected.get("time") != actual.get("time"):
        return "倒计时不同"
    lhs, rhs = expected.get("players", []), actual.get("players", [])
    if len(lhs) != 2 or len(rhs) != 2:
        return "场上角色数不同"
    for seat, (a, b) in enumerate(zip(lhs, rhs), 1):
        for field in ("id","hp","hpMax","x","y","state","facing","air","qi","energy"):
            x, y = a.get(field), b.get(field)
            if isinstance(x,(float,int)) and isinstance(y,(float,int)):
                if math.isclose(x, y, rel_tol=0, abs_tol=.002):
                    continue
            if x != y:
                return f"P{seat} {field} 记录值 {x}，重演值 {y}"
    return None


def _load(path):
    if path.suffix.lower() == ".bvndemo":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            header = json.loads(stream.readline())
            if header.get("type") != "demo" or header.get("format") != 1:
                raise ValueError("无法识别的数据回放格式。")
            return header["metadata"], MatchTape(json.loads(line) for line in stream)
    from replay_viewer import ReplayReader
    reader = ReplayReader(path)
    try:
        return reader.info["metadata"], MatchTape(event for _,event in reader.events())
    finally:
        reader.close()


def play_native(game_dir, source, status, stop):
    from referee import _open_game
    path = Path(source)
    metadata, tape = _load(path)
    expected_fingerprint = metadata.get("resourceFingerprint")
    if not matches_resource_fingerprint(game_dir, expected_fingerprint):
        raise ValueError("游戏资源指纹与比赛记录不一致。可打开同场的原画面回放。")
    mid = uuid.uuid4().hex
    with _open_game(Path(game_dir), stop, status) as game:
        for start in range(0, max(1, len(tape.inputs)), 120):
            game.send({"type":"tape","matchId":mid,"events":tape.inputs[start:start+120]})
        game.send(dict(tape.setup, matchId=mid, replay=True))
        origin = None
        checked = set()
        while True:
            msg = game.receive(time.monotonic() + 75)
            kind = msg["type"]
            if kind == "status":
                status(str(msg.get("message", ""))[:500])
                continue
            if msg.get("matchId") != mid:
                continue
            if kind == "result":
                if msg.get("winner") != tape.winner or len(checked) != len(tape.frames):
                    raise ValueError(f"结算或逻辑帧数量出现偏差，已核对 {len(checked)} / {len(tape.frames)} 帧。请打开同场原画面回放。")
                status(f"游戏重演结束。已核对 {len(checked)} 帧的双方状态和最终结果。")
                return len(checked)
            if kind == "frame":
                if origin is None:
                    origin = msg["tick"]
                offset = msg["tick"] - origin
                frame = tape.frames.get(offset)
                if frame is None:
                    difference = "记录中该时刻没有交战帧"
                else:
                    difference = compare_frame(frame, msg)
                if difference:
                    raise ValueError(f"第 {offset} 帧重演出现偏差（{difference}）。请打开同场原画面回放。")
                checked.add(offset)
