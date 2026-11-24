"""Read and replay an arena recording using only its local file."""
import bisect
from collections import OrderedDict
import ctypes as C
from ctypes import wintypes as W
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import time
import tkinter as tk
from tkinter import ttk, messagebox
import zlib

from PIL import Image, ImageChops, ImageTk
import zstandard
from replay import AUDIO_RATE, PAIR, TILE, _retain


def inflate(blob, maximum):
    d = zlib.decompressobj()
    data = d.decompress(blob,maximum+1)
    if len(data) > maximum or not d.eof:
        raise ValueError("回放数据损坏或超出格式限制")
    return data


class ReplayReader:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.db = sqlite3.connect(self.path.as_uri()+"?mode=ro",uri=True, timeout=2)
        try:
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.info = {k:json.loads(v) for k,v in self.db.execute("SELECT key,value FROM info")}
            if self.info.get("format") != "bvn-arena-replay/1":
                raise ValueError("回放格式不兼容")
            self.timeline = list(self.db.execute("SELECT us,id FROM frames ORDER BY us,id"))
            self.times = [v[0] for v in self.timeline]
            self.duration = max(self.info.get("durationUs",0),self.times[-1] if self.times else 0,
                                self.db.execute("SELECT coalesce(max(us),0) FROM events").fetchone()[0],
                                self.db.execute("SELECT coalesce(max(us + n * 1000000 / ?),0) FROM audio",(AUDIO_RATE,)).fetchone()[0])
        except Exception:
            self.db.close()
            raise
        self.cache, self.canvas, self.current = OrderedDict(), None, 0
        self.zstd = zstandard.ZstdDecompressor()

    def close(self):
        self.db.close()

    def events(self, kind=None):
        sql = "SELECT us,data FROM events"
        if kind is not None:
            sql += " WHERE kind=?"
        for us, blob in self.db.execute(sql+" ORDER BY id", (kind,) if kind is not None else ()):
            yield us, json.loads(inflate(blob, 8*1024*1024))

    def _tile(self, ref, w, h):
        hit = self.cache.get((ref,w,h))
        if hit is not None:
            self.cache.move_to_end((ref,w,h))
            return hit
        row = self.db.execute("SELECT data FROM tiles WHERE id=?", (ref,)).fetchone()
        if row is None:
            raise ValueError("回放缺少图块")
        im = Image.frombytes("RGB",(w,h),inflate(row[0],w*h*3))
        _retain(self.cache,(ref,w,h),im,4096)
        return im

    def _draw(self, row, check):
        seq,w,h,key,crc,encoded,residual = row
        if not (0<w<=8192 and 0<h<=8192 and w*h<=16777216):
            raise ValueError("回放尺寸无效")
        if key:
            self.canvas = Image.new("RGB",(w,h))
        if self.canvas is None or self.canvas.size != (w,h):
            raise ValueError("回放缺少关键帧")
        if residual is not None and residual.startswith(b"JPEG"):
            with Image.open(io.BytesIO(residual[4:]), formats=["JPEG"]) as decoded:
                if decoded.size != (w,h):
                    raise ValueError("回放压缩画面的尺寸不一致")
                self.canvas = decoded.convert("RGB")
        elif residual is not None:
            dx,dy = struct.unpack_from("<hh",residual)
            n = w*h
            payload = self.zstd.decompress(residual[4:],max_output_size=n*3)
            if len(payload) != n*3:
                raise ValueError("回放差分尺寸错误")
            g,r,b = (Image.frombytes("L",(w,h),payload[i:i+n]) for i in range(0,3*n,n))
            diff = Image.merge("RGB",(ImageChops.add_modulo(r,g),g,ImageChops.add_modulo(b,g)))
            self.canvas = ImageChops.add_modulo(ImageChops.offset(self.canvas,dx,dy),diff)
        cols = (w+TILE-1)//TILE
        total = cols*((h+TILE-1)//TILE)
        refs = inflate(encoded,total*PAIR.size)
        if key and len(refs) != total * PAIR.size:
            raise ValueError("回放关键帧不完整")
        for index, ref in PAIR.iter_unpack(refs):
            if index >= total:
                raise ValueError("回放图块位置错误")
            x,y = index%cols*TILE,index//cols*TILE
            self.canvas.paste(self._tile(ref,min(w-x,TILE),min(h-y,TILE)),(x,y))
        if check and zlib.crc32(self.canvas.tobytes()) != crc:
            raise ValueError(f"第 {seq} 个画面校验失败")
        self.current = seq

    def frame_at(self, us, check=True):
        i = bisect.bisect_right(self.times,us)-1
        if i < 0:
            return None
        target = self.timeline[i][1]
        if target == self.current:
            return self.canvas
        anchor = self.db.execute("SELECT max(id) FROM frames WHERE keyframe=1 AND id<=?",(target,)).fetchone()[0]
        if anchor is None:
            raise ValueError("回放缺少首帧")
        first = self.current+1 if anchor <= self.current < target else anchor
        sql = "SELECT id,w,h,keyframe,crc,refs,residual FROM frames WHERE id BETWEEN ? AND ? ORDER BY id"
        for row in self.db.execute(sql,(first,target)):
            self._draw(row,check)
        return self.canvas

    def audit(self):
        audit = {"fileBytes":self.path.stat().st_size,"durationUs":self.duration,
                 "frames":len(self.times),"tiles":self.db.execute("SELECT count(*) FROM tiles").fetchone()[0],
                 "maxGapUs":max((b-a for a,b in zip(self.times,self.times[1:])),default=0),
                 "firstPictureUs":self.times[0] if self.times else None,
                 "lastPictureUs":self.times[-1] if self.times else None,
                 "distinctFrames":self.db.execute("SELECT count(distinct crc) FROM frames").fetchone()[0],
                 "targetFps":self.info["presentation"]["targetFps"], "status":self.info["status"]}
        audit["audioSamples"] = self.db.execute("SELECT coalesce(sum(n),0) FROM audio").fetchone()[0]
        audit["frameEventRange"] = self.db.execute("SELECT min(us),max(us),count(*) FROM events WHERE kind='frame'").fetchone()
        audit["capture"] = [dict(event, us=us) for us,event in self.events("capture")]
        return audit

    def verify(self):
        frame_count = 0
        for row in self.db.execute("SELECT id,w,h,keyframe,crc,refs,residual FROM frames ORDER BY id"):
            self._draw(row,True)
            frame_count += 1
        return frame_count

    def audio_range(self, start, end):
        """Produce precisely timed stereo samples, filling genuine gaps with silence."""
        duration = round((end-start)*AUDIO_RATE/1e6)
        if duration <= 0:
            return b""
        out = bytearray(4*duration)
        sql = "SELECT us,n,data FROM audio WHERE us>=? AND us<? ORDER BY us,id"
        for us,n,encoded in self.db.execute(sql,(max(0,start-1_000_000),end)):
            if not 0 < n <= AUDIO_RATE:
                raise ValueError("回放音频包损坏")
            pos = round((us-start)*AUDIO_RATE/1e6)
            lo,hi = max(0,pos),min(duration,pos+n)
            if lo < hi:
                raw = inflate(encoded,n*4)
                if len(raw) != n*4:
                    raise ValueError("回放音频长度错误")
                out[lo*4:hi*4] = raw[(lo-pos)*4:(hi-pos)*4]
        return bytes(out)


class WaveHeader(C.Structure):
    _fields_ = [("data",C.c_void_p),("length",W.DWORD),("recorded",W.DWORD),
                ("user",C.c_size_t),("flags",W.DWORD),("loops",W.DWORD),
                ("next",C.c_void_p),("reserved",C.c_size_t)]


class AudioOutput:
    def __init__(self):
        self.dll = C.WinDLL("winmm")
        self.wave = C.c_void_p()
        self.active = []
        self.until = 0
        # Explicit pointer types keep wave device handles intact on 64-bit Windows.
        for name,args in {
            "waveOutOpen":[C.POINTER(C.c_void_p),W.UINT,C.c_void_p,C.c_size_t,C.c_size_t,W.DWORD],
            "waveOutPrepareHeader":[C.c_void_p,C.POINTER(WaveHeader),W.UINT],
            "waveOutWrite":[C.c_void_p,C.POINTER(WaveHeader),W.UINT],
            "waveOutUnprepareHeader":[C.c_void_p,C.POINTER(WaveHeader),W.UINT],
            "waveOutReset":[C.c_void_p],"waveOutClose":[C.c_void_p],
        }.items():
            getattr(self.dll,name).argtypes = args

    def reset(self):
        if self.wave:
            self.dll.waveOutReset(self.wave)
            for _,hdr in self.active:
                self.dll.waveOutUnprepareHeader(self.wave,C.byref(hdr),C.sizeof(hdr))
            self.dll.waveOutClose(self.wave)
        self.active = []
        self.wave = C.c_void_p()

    def start(self, us, speed):
        self.reset()
        rate = round(AUDIO_RATE*speed)
        fmt = struct.pack("<HHIIHHH",1,2,rate,rate*4,4,16,0)
        code = self.dll.waveOutOpen(C.byref(self.wave),0xFFFFFFFF,fmt,0,0,0)
        if code:
            raise OSError(f"无法打开音频输出（{code}）")
        self.until = us

    def pump(self, reader, us, speed):
        remaining = []
        for buf,hdr in self.active:
            if hdr.flags & 1:
                self.dll.waveOutUnprepareHeader(self.wave,C.byref(hdr),C.sizeof(hdr))
            else:
                remaining.append((buf,hdr))
        self.active = remaining
        if not self.active and self.until < us - 120_000*speed:
            self.until = us
        while self.until < min(reader.duration,us + 200_000*speed):
            end = min(reader.duration,self.until + int(75_000*speed))
            samples = reader.audio_range(self.until,end)
            self.until = end
            if not samples:
                break
            buf = C.create_string_buffer(samples)
            hdr = WaveHeader(data=C.addressof(buf),length=len(samples))
            if self.dll.waveOutPrepareHeader(self.wave,C.byref(hdr),C.sizeof(hdr)):
                raise OSError("音频缓冲准备失败")
            self.active.append((buf,hdr))
            if self.dll.waveOutWrite(self.wave,C.byref(hdr),C.sizeof(hdr)):
                raise OSError("音频播放失败")


def _clock(us):
    t = int(us / 1_000_000)
    return f"{t//60:02}:{t%60:02}"


class ReplayWindow:
    def __init__(self,parent,path):
        self.reader = ReplayReader(path)
        self.audit = self.reader.audit()
        self.win = tk.Toplevel(parent)
        self.win.title("原画面回放 · " + str(self.reader.info["metadata"].get("matchId","")))
        self.win.geometry("900x750")
        self.win.minsize(560,430)
        self.win.columnconfigure(0,weight=1)
        self.win.rowconfigure(0,weight=1)
        self.canvas = tk.Canvas(self.win,bg="#101622",highlightthickness=0)
        self.canvas.grid(row=0,column=0,sticky="nsew")
        self.picture_item = self.canvas.create_image(0,0,anchor="center")
        self.empty = self.canvas.create_text(0,0,text="",fill="white",font=("Microsoft YaHei UI",13))
        self.replay_badge = tk.Label(self.canvas, text="回放 · 原画面", font=("Microsoft YaHei UI",11,"bold"),
                                     foreground="#FFE5A1", background="#171920", padx=10, pady=3)
        self.replay_badge.place(relx=.5, y=10, anchor="n")
        bar = ttk.Frame(self.win,padding=(10,7))
        bar.grid(row=1,column=0,sticky="ew")
        bar.columnconfigure(1,weight=1)
        self.play = ttk.Button(bar,text="播放",command=self.toggle)
        self.play.grid(row=0,column=0,padx=(0,8))
        self.pos_var = tk.DoubleVar(value=0)
        self.slider = ttk.Scale(bar,from_=0,to=max(1,self.reader.duration),variable=self.pos_var,command=self.seek)
        self.slider.grid(row=0,column=1,sticky="ew")
        self.speed = tk.StringVar(value="1×")
        speeds = ttk.Combobox(bar,textvariable=self.speed,values=("0.5×","1×","1.5×","2×"),state="readonly",width=5)
        speeds.grid(row=0,column=2,padx=8)
        speeds.bind("<<ComboboxSelected>>",lambda e:self.seek(self.us))
        self.clock = ttk.Label(bar,width=14)
        self.clock.grid(row=0,column=3)
        ttk.Button(bar, text="赛后复盘", command=lambda: open_battle_report(self.win, self.reader.path)).grid(
            row=0, column=4, padx=(8, 0))
        self.status = ttk.Label(self.win,padding=(10,0,10,8),wraplength=850)
        self.status.grid(row=2,column=0,sticky="ew")
        self.running = False
        self.us = self.reader.times[0] if self.reader.times else 0
        self.origin = self.us
        self.epoch = time.perf_counter()
        self.last_display = None
        self.photo = None
        self.job = None
        self.audio = AudioOutput() if os.name == "nt" and self.audit["audioSamples"] else None
        self.audio_error = ""
        self.win.protocol("WM_DELETE_WINDOW",self.close)
        self.win.bind("<space>",lambda e:self.toggle())
        self.canvas.bind("<Configure>",lambda e:self.render(force=True))
        self.render(force=True)

    def speed_value(self):
        return float(self.speed.get().rstrip("×"))

    def sync_audio(self):
        if self.audio:
            try:
                self.audio.start(self.us,self.speed_value())
                self.audio.pump(self.reader,self.us,self.speed_value())
            except (OSError,ValueError) as exc:
                self.audio.reset()
                self.audio = None
                self.audio_error = str(exc)

    def seek(self, value):
        self.us = max(0,min(self.reader.duration,int(float(value))))
        self.origin,self.epoch = self.us,time.perf_counter()
        if self.running:
            self.sync_audio()
        self.render()

    def toggle(self):
        if self.running:
            self.running = False
            if self.job:
                self.win.after_cancel(self.job)
                self.job = None
            if self.audio:
                self.audio.reset()
            self.play.configure(text="播放")
        else:
            if self.us >= self.reader.duration:
                self.us = self.reader.times[0] if self.reader.times else 0
            self.running = True
            self.origin,self.epoch = self.us,time.perf_counter()
            self.play.configure(text="暂停")
            self.sync_audio()
            self.poll()

    def poll(self):
        self.job = None
        if not self.running:
            return
        speed = self.speed_value()
        self.us = min(self.reader.duration,self.origin+int((time.perf_counter()-self.epoch)*1e6*speed))
        if self.audio:
            try:
                self.audio.pump(self.reader,self.us,speed)
            except (OSError,ValueError) as exc:
                self.audio.reset()
                self.audio = None
                self.audio_error = str(exc)
        self.render()
        if self.us >= self.reader.duration and self.running:
            self.toggle()
        elif self.running:
            self.job = self.win.after(12,self.poll)

    def render(self,force=False):
        w,h = max(2,self.canvas.winfo_width()),max(2,self.canvas.winfo_height())
        idx = bisect.bisect_right(self.reader.times,self.us)-1
        if force or (idx,w,h) != self.last_display:
            self.last_display = idx,w,h
            try:
                img = self.reader.frame_at(self.us)
                if img is not None:
                    ratio = min(w/img.width,h/img.height)
                    size = max(1,round(img.width*ratio)),max(1,round(img.height*ratio))
                    self.photo = ImageTk.PhotoImage(img if size == img.size else img.resize(size,Image.Resampling.NEAREST))
                    self.canvas.itemconfigure(self.picture_item,image=self.photo)
                    self.canvas.itemconfigure(self.empty,text="")
                else:
                    self.canvas.itemconfigure(self.picture_item,image="")
                    self.canvas.itemconfigure(self.empty,text="此时段没有游戏画面")
                for item in (self.picture_item,self.empty):
                    self.canvas.coords(item,w//2,h//2)
            except Exception as exc:
                if self.running:
                    self.toggle()
                self.canvas.itemconfigure(self.empty,text="画面读取失败\n"+str(exc))
        self.pos_var.set(self.us)
        self.clock.configure(text=f"{_clock(self.us)} / {_clock(self.reader.duration)}")
        self.status.configure(text=self.summary())

    def summary(self):
        a = self.audit
        flags = []
        if a["status"] != "finished":
            flags.append("录制中断或尚未结束")
        if not a["frames"]:
            flags.append("未取得画面")
        if not a["audioSamples"]:
            flags.append("无游戏音轨")
        if self.audio_error:
            flags.append(self.audio_error)
        ends = [event for event in a["capture"] if event.get("status") == "capture_end"]
        dropped = sum(e.get("dropped",0)+e.get("missedSamples",0) for e in ends)
        failures = sum(e.get("failedSamples",0) for e in ends)
        if a["maxGapUs"] > 130_000 or dropped or failures:
            flags.append(f"漏采 {dropped} 次，窗口读取失败 {failures} 次，最大画面间隔 {a['maxGapUs']/1000:.0f} ms")
        if any(e.get("audioDiscontinuities",0) for e in ends):
            flags.append("音轨有时间中断")
        first,last,count = a["frameEventRange"]
        if count and (not a["frames"] or a["firstPictureUs"] > first+100_000 or a["lastPictureUs"]+100_000 < last):
            flags.append("部分对战时段缺少画面")
        result = self.reader.info.get("result",{})
        winner = result.get("winnerName") if isinstance(result,dict) else None
        if winner:
            flags.insert(0,"胜者 "+str(winner))
        base = f"窗口原画面 · {a['frames']} 个采样 · {a['fileBytes']/1048576:.1f} MiB"
        return "  |  ".join([base,*flags])

    def close(self):
        if self.job:
            self.win.after_cancel(self.job)
        if self.audio:
            self.audio.reset()
        self.reader.close()
        self.win.destroy()


def open_replay(parent,path):
    try:
        return ReplayWindow(parent,path)
    except (OSError,sqlite3.Error,ValueError,KeyError) as exc:
        messagebox.showerror("无法打开回放",str(exc),parent=parent)
        return None


def open_battle_report(parent, path):
    from battle_report import FORMAT, SUFFIX, render_report, write_report

    path = Path(path).resolve()
    report = path.with_suffix(SUFFIX)
    try:
        if not report.is_file():
            archive = path.with_suffix(".bvnreplay")
            if not archive.is_file():
                raise ValueError("请将同一场比赛的 .bvnreplay 或 .bvnreport.json 放在此回放旁。")
            report = write_report(archive)
        data = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("复盘报告格式不兼容。")
        content = render_report(data)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        messagebox.showerror("无法打开赛后复盘", str(exc), parent=parent)
        return None

    win = tk.Toplevel(parent)
    win.title("赛后复盘")
    win.geometry("790x580")
    win.minsize(480, 330)
    body = ttk.Frame(win, padding=(12, 12, 12, 0))
    body.pack(fill="both", expand=True)
    scrollbar = ttk.Scrollbar(body, orient="vertical")
    scrollbar.pack(side="right", fill="y")
    text = tk.Text(body, wrap="word", font=("Microsoft YaHei UI", 10), padx=10, pady=8,
                   yscrollcommand=scrollbar.set)
    text.pack(fill="both", expand=True)
    scrollbar.configure(command=text.yview)
    text.insert("1.0", content)
    text.configure(state="disabled")
    path_bar = ttk.Frame(win, padding=12)
    path_bar.pack(fill="x")
    ttk.Label(path_bar, text="AI 报告").pack(side="left", padx=(0, 8))
    location = ttk.Entry(path_bar)
    location.insert(0, str(report))
    location.configure(state="readonly")
    location.pack(side="left", fill="x", expand=True)
    return win
