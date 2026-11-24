"""Arena entrant manifests and paired, deadline-bound JSONL exchanges."""
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


BUTTONS = {"left", "right", "forward", "back", "up", "down", "attack",
           "jump", "dash", "skill", "super", "assist"}
MAX_LINE = 64 * 1024


@dataclass(frozen=True)
class Team:
    name: str
    color: str
    directory: Path
    command: tuple
    fingerprint: str


class BotFailure(Exception):
    """One entrant stopped or failed its protocol."""


def buttons(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("buttons 应为按键名列表")
    if any(type(item) is not str or item not in BUTTONS for item in value):
        raise ValueError("存在未知按键")
    for first, second in [("left", "right"), ("up", "down"), ("forward", "back")]:
        if first in value and second in value:
            raise ValueError("同帧方向相反")
    if {"left","right"}.intersection(value) and {"forward","back"}.intersection(value):
        raise ValueError("同帧混用绝对与相对水平方向")
    return list(dict.fromkeys(value))


def load_team(path):
    path = Path(path).resolve()
    if path.is_dir():
        path = next((path / f for f in ("entrant.json", "team.json") if (path / f).is_file()),
                    path / "entrant.json")
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict):
        raise ValueError("参赛清单必须是 JSON 对象")
    if spec.get("protocol", 1) != 1:
        raise ValueError("参赛清单的协议版本必须为 1")
    name, color = spec.get("name"), spec.get("color")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32 or not name.isprintable():
        raise ValueError("队名应为 1 至 32 个可显示字符")
    if not isinstance(color, str) or not re.fullmatch("#[0-9A-Fa-f]{6}", color):
        raise ValueError("队色应为 #RRGGBB")
    engine = spec.get("engine", "executable")
    if engine == "bundled-python":
        entry = spec.get("script")
        prefix = ([sys.executable, "--entrant-script"] if getattr(sys, "frozen", False)
                  else [sys.executable, "-u"])
    elif engine == "executable":
        entry = spec.get("executable")
        prefix = []
    else:
        raise ValueError("不支持的参赛引擎")
    if not isinstance(entry, str) or not entry or Path(entry).is_absolute():
        raise ValueError("入口须填写参赛目录内的相对路径")
    target = (path.parent / entry).resolve()
    if not target.is_relative_to(path.parent) or not target.is_file():
        raise ValueError("参赛入口不存在或不在参赛目录内")
    if engine == "bundled-python" and target.suffix.lower() != ".py":
        raise ValueError("bundled-python 入口应为 .py 文件")
    if engine == "executable" and os.name == "nt" and target.suffix.lower() != ".exe":
        raise ValueError("Windows executable 入口应为 .exe 文件")
    args = spec.get("args", [])
    if not isinstance(args, list) or any(type(arg) is not str for arg in args):
        raise ValueError("args 应为字符串数组")
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    digest.update(path.read_bytes())
    # A launcher can take the actual bot script or its configuration as an argument.
    sources = set()
    for arg in args:
        filename = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        try:
            candidate = (path.parent / filename).resolve()
            if candidate != target and candidate.is_relative_to(path.parent) and candidate.is_file():
                sources.add(candidate)
        except (OSError, ValueError, RuntimeError):
            continue
    for source in sorted(sources):
        digest.update(b"\0" + source.relative_to(path.parent).as_posix().encode("utf-8") + b"\0")
        with source.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return Team(name.strip(), color.upper(), path.parent, tuple(prefix + [str(target)] + args),
                digest.hexdigest())


class BotProcess:
    def __init__(self, team):
        self.team = team
        self.failure = None
        self._responses = queue.Queue(128)
        self._outgoing = queue.Queue(1)
        self._closed = threading.Event()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(team.command, cwd=team.directory, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._reader.start()
        self._writer.start()

    def _read(self):
        try:
            while not self._closed.is_set():
                line = self.proc.stdout.readline(MAX_LINE + 1)
                if not line:
                    raise BotFailure("AI 已退出或关闭输出")
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise BotFailure("AI 响应超过 64 KiB 或缺少换行")
                obj = json.loads(line.decode("utf-8"))
                if not isinstance(obj, dict):
                    raise BotFailure("AI 响应应为 JSON 对象")
                self._responses.put_nowait((time.monotonic(), obj))
        except (OSError, ValueError, queue.Full, BotFailure) as exc:
            self.failure = BotFailure(str(exc) or "AI 输出积压")

    def _write(self):
        try:
            while not self._closed.is_set():
                packet = self._outgoing.get()
                if packet is None:
                    break
                self.proc.stdin.write(packet)
                self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self.failure = BotFailure(str(exc))

    def submit(self, message):
        packet = json.dumps(message, ensure_ascii=False, separators=(",",":"), allow_nan=False)
        try:
            self._outgoing.put_nowait(packet.encode("utf-8") + b"\n")
        except queue.Full:
            # Keep newest state when an entrant has stopped consuming observations.
            try:
                self._outgoing.get_nowait()
            except queue.Empty:
                pass
            self._outgoing.put_nowait(packet.encode("utf-8") + b"\n")

    def poll(self, query, deadline):
        expected_type = "pick" if query["type"] == "draft" else "action"
        while True:
            try:
                received_at, reply = self._responses.get_nowait()
            except queue.Empty:
                return self.failure
            if received_at > deadline or reply.get("requestId") != query["requestId"]:
                continue
            if reply.get("type") != expected_type:
                continue
            if expected_type == "action" and (type(reply.get("tick")) is not int or reply["tick"] != query["tick"]):
                continue
            return reply

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._outgoing.put_nowait(None)
        except queue.Full:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._writer.join(.5)
        self._reader.join(.5)
        # A child process inheriting the entrant stdout can hold a reader open.
        # Avoid blocking shutdown on that foreign process.
        if not self._writer.is_alive():
            self.proc.stdin.close()
        if not self._reader.is_alive():
            self.proc.stdout.close()


def exchange(runners, queries, seconds, stop=None):
    """Publish both seats together. Only matching, timely replies count; None is timeout/CPU."""
    request_id = secrets.token_hex(12)
    replies = [None, None]
    pending = set()
    deadline = time.monotonic() + seconds
    for i, runner in enumerate(runners):
        if runner is None or queries[i] is None:
            continue
        queries[i] = dict(queries[i], requestId=request_id)
        runner.submit(queries[i])
        pending.add(i)
    while pending:
        if stop is not None and stop.is_set():
            raise InterruptedError("主持人停止了比赛")
        for i in tuple(pending):
            reply = runners[i].poll(queries[i], deadline)
            if reply is not None:
                replies[i] = reply
                pending.remove(i)
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            break
        time.sleep(min(.001, remaining))
    return replies
