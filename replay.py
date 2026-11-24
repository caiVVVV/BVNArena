"""Durable referee events and timed, lossless original-window presentation samples."""
from collections import OrderedDict
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
import zlib

from PIL import Image, ImageChops
from replay_assets.window import WindowCapture
import zstandard

TILE = 32
DEFAULT_FPS = 30
KEY_INTERVAL = 5_000_000
AUDIO_RATE = 44100
PAIR = struct.Struct("<II")
SCHEMA = """
    CREATE TABLE info(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE events(id INTEGER PRIMARY KEY, us INTEGER NOT NULL, kind TEXT, tick INTEGER, data BLOB NOT NULL);
    CREATE INDEX event_kind ON events(kind,us);
    CREATE TABLE tiles(id INTEGER PRIMARY KEY, hash BLOB NOT NULL UNIQUE, data BLOB NOT NULL);
    CREATE TABLE frames(id INTEGER PRIMARY KEY, us INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
        keyframe INTEGER NOT NULL, crc INTEGER NOT NULL, refs BLOB NOT NULL, residual BLOB);
    CREATE INDEX frame_time ON frames(us);
    CREATE INDEX frame_key ON frames(keyframe,id);
    CREATE TABLE audio(id INTEGER PRIMARY KEY, us INTEGER NOT NULL, n INTEGER NOT NULL, data BLOB NOT NULL);
    CREATE INDEX audio_time ON audio(us);
"""


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _retain(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    if len(cache) > limit:
        cache.popitem(last=False)


class TileEncoder:
    def __init__(self):
        self.previous = None
        self.grid = []
        self.cache = OrderedDict()
        self.last_key = -KEY_INTERVAL
        self.compressor = zstandard.ZstdCompressor(level=3)
        self.compact_motion = False
        self.jpeg_interval = False

    @staticmethod
    def _scroll(current, previous):
        # Find the playfield's dominant translation, ignoring HUD and wrapped edges.
        w, h = current.size
        if w < 128 or h < 96:
            return 0, 0
        box = w//4, h//3, 3*w//4, 3*h//4
        tiny = max(8,w//12), max(8,h//12)
        a, b = [frame.convert("L").resize(tiny, Image.Resampling.BILINEAR) for frame in (current,previous)]
        def cost(front, back):
            hist = ImageChops.difference(front, back).histogram()
            return sum(i*n for i,n in enumerate(hist))
        box_small = tuple(round(v/12) for v in box)
        sample = a.crop(box_small)
        def tiny_cost(dx, dy):
            l,t,r,d = box_small
            return cost(sample, b.crop((l-dx,t-dy,r-dx,d-dy)))
        dx = min(range(-4,5), key=lambda x:tiny_cost(x,0))
        dy = min(range(-3,4), key=lambda y:tiny_cost(dx,y))
        dx, dy = dx*12, dy*12
        # A pair of thin strips makes integer refinement inexpensive.
        l,t,r,d = box
        row = (l, (t+d)//2, r, (t+d)//2+3)
        col = ((l+r)//2,t,(l+r)//2+3,d)
        def refine(crop, x0, y0, horizontal):
            sample = current.crop(crop).convert("L")
            def rank(v):
                x,y = (v,y0) if horizontal else (x0,v)
                x1,y1,x2,y2 = crop
                return cost(sample, previous.crop((x1-x,y1-y,x2-x,y2-y)).convert("L"))
            center = x0 if horizontal else y0
            return min(range(center-8,center+9),key=lambda v:(rank(v),abs(v)))
        dx = refine(row,dx,dy,True)
        dy = refine(col,dx,dy,False)
        return dx,dy

    def _residual(self, im, difference):
        # Small motions and lighting changes are stored as exact signed-modulo RGB differences.
        dx, dy = self._scroll(im, self.previous)
        residual = ImageChops.subtract_modulo(im,self.previous)
        if dx or dy:
            shifted = ImageChops.offset(self.previous,dx,dy)
            alt = ImageChops.subtract_modulo(im,shifted)
            score = lambda x: x.resize((64,48),Image.Resampling.NEAREST).tobytes().count(0)
            if score(alt) > score(residual):
                residual = alt
            else:
                dx, dy = 0, 0
        # Separate channels to keep matching regions contiguous for the entropy coder.
        r,g,b = residual.split()
        raw = g.tobytes() + ImageChops.subtract_modulo(r,g).tobytes() + ImageChops.subtract_modulo(b,g).tobytes()
        return struct.pack("<hh",dx,dy) + self.compressor.compress(raw)

    @staticmethod
    def _compact_picture(im):
        # Keep moving scenes reasonably sized even when virtually every pixel changes.
        for quality in (83, 69, 54):
            output = io.BytesIO()
            im.save(output, format="JPEG", quality=quality, subsampling=2, optimize=True)
            body = output.getvalue()
            if len(body) <= 40 * 1024:
                break
        output.seek(0)
        with Image.open(output) as decoded:
            return b"JPEG"+body, zlib.crc32(decoded.tobytes())

    def store(self, recorder, picture, us):
        im = picture if picture.mode == "RGB" else picture.convert("RGB")
        w, h = im.size
        if w < 1 or h < 1 or w * h > 16777216:
            raise ValueError("回放画面尺寸无效")
        cols = (w + TILE - 1) // TILE
        resized = self.previous is None or self.previous.size != im.size
        key = resized or us - self.last_key >= KEY_INTERVAL
        difference = None if resized else ImageChops.difference(im,self.previous)
        area = (0, 0, w, h) if key else difference.getbbox()
        if key:
            self.jpeg_interval = False
        broad = False
        if not key and area and (area[2]-area[0])*(area[3]-area[1]) > w*h//4:
            preview = difference.resize((64,48),Image.Resampling.NEAREST)
            broad = preview.tobytes().count(0) < 64*48*3*.76
        residual = None
        if self.compact_motion and (self.jpeg_interval or broad) and area:
            residual, crc = self._compact_picture(im)
            self.jpeg_interval = True
        if broad and residual is None:
            residual = self._residual(im,difference)
        if residual:
            # Future sparse tile references must compare against the actual previous image.
            self.grid = [0] * len(self.grid)
        updates, new_tiles = {}, {}
        if resized:
            self.grid = [0] * (cols * ((h + TILE - 1) // TILE))
        if area and residual is None:
            x0, y0, x1, y1 = area
            for row in range(y0 // TILE, (y1 + TILE - 1) // TILE):
                y = row * TILE
                for col in range(x0 // TILE, (x1 + TILE - 1) // TILE):
                    x = col * TILE
                    tile = im.crop((x, y, min(x + TILE, w), min(y + TILE, h)))
                    raw = tile.tobytes()
                    digest = hashlib.blake2b(bytes(tile.size) + raw, digest_size=16).digest()
                    index = row * cols + col
                    cached = self.cache.get(digest)
                    if cached is not None:
                        self.cache.move_to_end(digest)
                        if self.grid[index] != cached:
                            updates[index] = cached
                    else:
                        if digest not in new_tiles:
                            new_tiles[digest] = zlib.compress(raw, 3)
                        updates[index] = digest
        if residual is None or not self.jpeg_interval:
            crc = zlib.crc32(im.tobytes()) if area else self.last_crc
        with recorder.lock:
            if recorder.closed:
                return
            with recorder.db as db:
                ids = {}
                for digest, encoded in new_tiles.items():
                    row = db.execute("INSERT INTO tiles(hash,data) VALUES(?,?) ON CONFLICT(hash) DO NOTHING RETURNING id",
                                     (digest, encoded)).fetchone()
                    tile_id = row[0] if row else db.execute("SELECT id FROM tiles WHERE hash=?", (digest,)).fetchone()[0]
                    ids[digest] = tile_id
                    _retain(self.cache, digest, tile_id, 32768)
                delta = []
                for index, ref in updates.items():
                    ref = ids[ref] if isinstance(ref, bytes) else ref
                    if ref != self.grid[index]:
                        self.grid[index] = ref
                        delta.append((index, ref))
                refs = enumerate(self.grid) if key else delta
                packed = zlib.compress(b"".join(PAIR.pack(index, ref) for index, ref in refs), 3)
                db.execute("INSERT INTO frames(us,w,h,keyframe,crc,refs,residual) VALUES(?,?,?,?,?,?,?)",
                           (us, w, h, int(key), crc, packed, residual))
        self.previous, self.last_crc = im.copy(), crc
        if key:
            self.last_key = us


class CaptureSession:
    def __init__(self, recorder, pid, hwnd):
        self.rec = recorder
        self.pid, self.hwnd = pid, hwnd
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.sample_done = threading.Event()
        self.pending = queue.Queue(maxsize=4)
        self.helper = None
        self.helper_lock = threading.Lock()
        self.dropped = self.missed = self.audio_gaps = self.sample_errors = 0
        self.last_issue = None
        self.fps = DEFAULT_FPS
        self.threads = [
            threading.Thread(target=self.encode, name="arena-frame-store", daemon=True),
            threading.Thread(target=self.sample, name="arena-window-sampler", daemon=True),
            threading.Thread(target=self.audio, name="arena-process-audio", daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def note(self, kind, **details):
        self.rec.append({"type": "capture", "status": kind, **details})

    def sample(self):
        cap = None
        try:
            cap = WindowCapture(self.pid, self.hwnd)
            interval = 1 / self.fps
            due = time.perf_counter()
            bound = False
            while not self.stop.is_set():
                try:
                    img = cap.grab()
                    us = self.rec.elapsed()
                    if self.stop.is_set():
                        break
                    if not bound or self.last_issue:
                        self.note("window", hwnd=cap.find(), width=img.width, height=img.height)
                        bound, self.last_issue = True, None
                    try:
                        self.pending.put_nowait((us, img))
                    except queue.Full:
                        self.dropped += 1
                except OSError as exc:
                    issue = str(exc)
                    self.sample_errors += 1
                    if self.last_issue != issue:
                        self.note("picture_unavailable", reason=issue)
                        self.last_issue = issue
                    self.stop.wait(0.10)
                    due = time.perf_counter()
                    continue
                due += interval
                now = time.perf_counter()
                if now > due + interval:
                    lag = int((now - due) / interval)
                    self.missed += lag
                    due += lag * interval
                self.stop.wait(max(0, due - now))
        except Exception as exc:
            self.note("picture_unavailable", reason=str(exc))
        finally:
            if cap:
                cap.close()
            self.sample_done.set()

    def encode(self):
        try:
            while not self.sample_done.is_set() or not self.pending.empty():
                try:
                    us, im = self.pending.get(timeout=.1)
                except queue.Empty:
                    continue
                self.rec.store_picture(im, us)
                self.ready.set()
        except Exception as exc:
            self.note("picture_store_error", reason=str(exc))
            self.stop.set()

    def audio(self):
        exe = Path(__file__).resolve().parent / "replay_assets" / "ProcessAudio.exe"
        proc = None
        pending = bytearray()
        first = None
        def flush():
            nonlocal first
            if pending:
                self.rec.store_audio(first, bytes(pending))
                pending.clear()
            first = None
        try:
            with self.helper_lock:
                if self.stop.is_set():
                    return
                proc = self.helper = subprocess.Popen(
                    [str(exe), str(self.pid), str(os.getpid())], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            while True:
                header = proc.stdout.read(16)
                if len(header) != 16:
                    if not self.stop.is_set():
                        raise OSError(proc.stderr.read(2048).decode(errors="replace") or "音频进程退出")
                    break
                qpc, n, flags = struct.unpack("<QII", header)
                if n == 0:
                    self.note("audio_ready", rate=AUDIO_RATE, channels=2)
                    continue
                if n > AUDIO_RATE:
                    raise ValueError("音频数据包过大")
                raw = proc.stdout.read(n*4)
                if len(raw) != n*4:
                    break
                us = max(0, qpc // 10 - self.rec.origin_us)
                if flags & 4 or not qpc:
                    us = max(0, self.rec.elapsed() - round(n * 1e6 / AUDIO_RATE))
                if first is not None:
                    expected = first + round(len(pending) * 1e6 / (4 * AUDIO_RATE))
                    if abs(us - expected) > 5000 or flags & 1:
                        flush()
                        self.audio_gaps += 1
                if first is None:
                    first = us
                pending.extend(raw)
                if len(pending) >= 4*AUDIO_RATE//8:
                    flush()
        except Exception as exc:
            if not self.stop.is_set():
                self.note("audio_unavailable", reason=str(exc))
        finally:
            flush()
            if proc:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=2)
                proc.stdout.close()
                proc.stderr.close()

    def finish(self):
        self.stop.set()
        with self.helper_lock:
            if self.helper and self.helper.poll() is None:
                self.helper.terminate()
        for thread in self.threads[1:]:
            thread.join(timeout=1.5)
        if not self.sample_done.is_set():
            self.sample_done.set()
            self.note("picture_timeout")
        self.threads[0].join(timeout=4)
        if self.threads[0].is_alive():
            self.note("picture_store_timeout", queued=self.pending.qsize())
        self.note("capture_end", dropped=self.dropped, missedSamples=self.missed,
                  failedSamples=self.sample_errors, audioDiscontinuities=self.audio_gaps)


class ArenaRecording:
    def __init__(self, data_dir: Path, metadata: dict):
        folder = Path(data_dir) / "replays"
        folder.mkdir(parents=True, exist_ok=True)
        match = re.sub(r"[^\w.-]", "_", str(metadata.get("matchId", "match")))[:75].strip(" .")
        self.path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{match}_{uuid.uuid4().hex[:8]}.bvnreplay"
        self.lock = threading.RLock()
        self.end_lock, self.frame_lock = threading.Lock(), threading.Lock()
        self.origin_us = time.perf_counter_ns() // 1000
        self.closed = False
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        # With a rollback journal, every committed frame is already in the shareable main file.
        self.db.execute("PRAGMA journal_mode=TRUNCATE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.encoder = TileEncoder()
        self.session = None
        with self.db:
            self._info("format", "bvn-arena-replay/1")
            self._info("metadata", metadata)
            self._info("status", "recording")
            self._info("created", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            self._info("presentation", {"method":"window_tile_delta","tile":TILE,"targetFps":DEFAULT_FPS})

    def elapsed(self):
        return time.perf_counter_ns() // 1000 - self.origin_us

    def _info(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO info VALUES(?,?)", (key, compact(value)))

    def append(self, event: dict):
        us = self.elapsed()
        data = zlib.compress(compact(event).encode(), 3)
        tick = event.get("tick")
        with self.lock:
            if not self.closed:
                with self.db:
                    self.db.execute("INSERT INTO events(us,kind,tick,data) VALUES(?,?,?,?)", (
                        us, str(event.get("type", "event")), tick if isinstance(tick,int) else None, data))

    def bind_game(self, pid: int, hwnd: int | None = None):
        with self.end_lock:
            if self.closed:
                return
            if self.session and (self.session.pid, self.session.hwnd) == (pid, hwnd):
                return
            if self.session:
                self.session.finish()
            self.append({"type":"capture","status":"bind","pid":pid,"hwnd":hwnd})
            self.encoder.compact_motion = True
            with self.lock, self.db:
                self._info("presentation", {"method":"window_tile_delta", "tile":TILE,
                                            "targetFps":DEFAULT_FPS, "motion":"adaptive-jpeg"})
            self.session = CaptureSession(self, pid, hwnd)

    def wait_for_picture(self, timeout=8):
        return bool(self.session and self.session.ready.wait(timeout))

    def store_picture(self, picture, us):
        with self.frame_lock:
            self.encoder.store(self, picture, int(us))

    def store_audio(self, us, pcm):
        if not pcm or len(pcm) % 4:
            return
        encoded = zlib.compress(pcm, 3)
        with self.lock:
            if not self.closed:
                with self.db:
                    self.db.execute("INSERT INTO audio(us,n,data) VALUES(?,?,?)", (us,len(pcm)//4,encoded))

    def _end(self, status, result):
        with self.end_lock:
            if self.closed:
                return
            if self.session:
                self.session.finish()
            with self.lock:
                with self.db:
                    self._info("durationUs", self.elapsed())
                    self._info("status", status)
                    self._info("result", result)
                self.closed = True
                self.db.close()
            # TRUNCATE mode leaves an empty sidecar after its last successful commit.
            journal = Path(str(self.path) + "-journal")
            if journal.is_file() and journal.stat().st_size == 0:
                journal.unlink(missing_ok=True)
        return self.path

    def finish(self, result: dict):
        return self._end("finished", result)

    def interrupt(self, reason: str):
        return self._end("interrupted", {"reason":str(reason)})
