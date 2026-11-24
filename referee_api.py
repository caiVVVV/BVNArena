"""File-based automation for the local BVN referee."""
import json
from pathlib import Path
import threading
import time

from installation import check_game, list_maps
from referee import _atomic_json, _utc, run_series


STATUS_FILE = "status.json"
STOP_FILE = "stop.requested"
JOB_STATES = ("running", "completed", "stopped", "failed")
POLL_SECONDS = .2


class RequestError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(code, exc):
    return {"code": code, "message": str(exc)}


def _write_json(path, value):
    # A Windows reader may hold the previous snapshot open during replacement.
    deadline = time.monotonic() + 1
    while True:
        try:
            _atomic_json(path, value)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.02)


def _text(request, key):
    value = request.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RequestError("invalid_request", f"{key} 须为非空字符串")
    return value


def _path(request, key, base):
    return (base / _text(request, key)).resolve()


def _game_path(request, base, default_game):
    if "gameDir" in request:
        return _path(request, "gameDir", base)
    if default_game is None:
        raise RequestError("invalid_request", "找不到默认游戏目录，请指定 gameDir")
    return Path(default_game).resolve()


def _read_status(job_dir):
    try:
        status = json.loads((job_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RequestError("job_not_found", f"找不到任务状态 {job_dir}") from exc
    except ValueError as exc:
        raise RequestError("invalid_job_status", f"无法读取任务状态 {job_dir}") from exc
    if not isinstance(status, dict) or status.get("state") not in JOB_STATES:
        raise RequestError("invalid_job_status", f"无法识别任务状态 {job_dir}")
    return status


def _job_status(job_dir, request_stop):
    status = _read_status(job_dir)
    stop_path = job_dir / STOP_FILE
    if request_stop and status["state"] == "running":
        try:
            with stop_path.open("x"):
                pass
        except FileExistsError:
            pass
        status = _read_status(job_dir)
    if status["state"] == "running":
        status["stopRequested"] = stop_path.exists()
    return status


def _watch_stop(stop_path, stop, finished):
    while not finished.is_set():
        if stop_path.exists():
            stop.set()
            return
        finished.wait(POLL_SECONDS)


def _run_job(request, base, job_dir, default_game):
    game_dir = _game_path(request, base, default_game)
    team_a = _path(request, "teamA", base)
    team_b = None if "teamB" in request and request["teamB"] is None else _path(request, "teamB", base)
    map_id = _text(request, "map")
    data_dir = _path(request, "dataDir", base) if "dataDir" in request else game_dir / "AIArena/data"
    skip_cut_in = request.get("skipCutIn", False)
    if type(skip_cut_in) is not bool:
        raise RequestError("invalid_request", "skipCutIn 须为布尔值")

    try:
        job_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise RequestError("job_exists", f"任务目录已存在，请选用新目录 {job_dir}") from exc

    stop, finished = threading.Event(), threading.Event()
    stop_path = job_dir / STOP_FILE
    status = {"jobDir": str(job_dir), "gameDir": str(game_dir), "dataDir": str(data_dir),
              "state": "running", "startedAt": _utc()}

    def update(message):
        status.update(message=message, updatedAt=_utc(), stopRequested=stop_path.exists())
        _write_json(job_dir / STATUS_FILE, status)

    update("正在准备比赛")
    watcher = threading.Thread(target=_watch_stop, args=(stop_path, stop, finished), daemon=True)
    watcher.start()
    try:
        result = run_series(game_dir, team_a, team_b, map_id, data_dir, update, stop,
                            skip_cut_in=skip_cut_in)
        status.update(state="completed", result=result)
        code, message = 0, "比赛已完成"
    except Exception as exc:
        if isinstance(exc, InterruptedError) and stop.is_set():
            status.update(state="stopped", error=_error("stopped", exc))
            code = 2
        else:
            status.update(state="failed", error=_error("run_failed", exc))
            code = 1
        message = str(exc)
    finally:
        finished.set()
        watcher.join()
    update(message)
    return code, status


def handle_request(request_file, response_file, *, default_game=None):
    """Write a JSON response and return the CLI exit code for this invocation."""
    try:
        response_file = Path(response_file).resolve()
    except (OSError, RuntimeError, ValueError):
        return 1
    if response_file.suffix.casefold() != ".json" or response_file.name.casefold() == STATUS_FILE:
        return 1
    op = None
    try:
        request_file = Path(request_file).resolve()
        if request_file == response_file:
            return 1
        try:
            request = json.loads(request_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RequestError("invalid_request", f"无法读取请求文件 {exc}") from exc
        if not isinstance(request, dict):
            raise RequestError("invalid_request", "请求须为 JSON 对象")
        op = _text(request, "op")
        base = request_file.parent
        code = 0
        if op == "list_maps":
            game_dir = check_game(_game_path(request, base, default_game))
            payload = {"gameDir": str(game_dir),
                       "maps": [{"id": key, "name": name} for key, name in list_maps(game_dir)]}
        elif op in ("run", "status", "stop"):
            job_dir = _path(request, "jobDir", base)
            if op == "run":
                code, payload = _run_job(request, base, job_dir, default_game)
            else:
                payload = _job_status(job_dir, request_stop=(op == "stop"))
        else:
            raise RequestError("invalid_request", f"未知操作 {op}")
    except RequestError as exc:
        code, payload = 1, {"error": _error(exc.code, exc)}
    except (OSError, RuntimeError, ValueError) as exc:
        code, payload = 1, {"error": _error("request_failed", exc)}

    try:
        _write_json(response_file, {**payload, "op": op, "ok": code == 0})
    except OSError:
        return 1
    return code
