"""Desktop entry point, installer, and headless entry points for tournament use."""
import argparse
import io
import json
import os
from pathlib import Path
import queue
import runpy
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from installation import BUILD, ENTRANTS, MANAGER, check_game, deploy, export_training_kit, list_maps, remove_installation
from installation import PREFERRED_ASSIST, default_roster, list_human_choices

RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FROZEN = bool(getattr(sys, "frozen", False))
C = {"ink": "#152C3C", "teal": "#08786E", "paper": "#F4F5F0", "white": "#FFFFFF", "muted": "#536572"}
FONT = "Microsoft YaHei UI"
MATCH_MODES = ("选手对选手（七局四胜）", "玩家对 AI（单局）", "选手对游戏 CPU（单局）")


def entrant_name(path):
    try:
        manifest = json.loads((Path(path) / "entrant.json").read_text(encoding="utf-8-sig"))
        return str(manifest["name"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def game_hint():
    candidates = [Path(sys.executable).parent.parent, Path.cwd()]
    return next((p for p in candidates if (p / "Bleach vs. Naruto.exe").is_file()), None)


def installer_manager():
    core = RESOURCES / "core/BVNArena-Setup.exe"
    return core if core.is_file() else Path(sys.executable)


def bundled_script(script, arguments=()):
    # Windowed bootloaders can leave the Python streams unset even when Win32
    # inherited valid pipes from the referee. Open duplicates, never the console.
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        for name, code, mode in (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w")):
            if getattr(sys, name) is None:
                h = k.GetStdHandle(code)
                if h and h != ctypes.c_void_p(-1).value:
                    duplicate = wintypes.HANDLE()
                    proc = k.GetCurrentProcess()
                    if k.DuplicateHandle(proc, h, proc, ctypes.byref(duplicate), 0, False, 2):
                        fd = msvcrt.open_osfhandle(duplicate.value, os.O_BINARY | (os.O_RDONLY if mode == "r" else os.O_WRONLY))
                        stream = io.TextIOWrapper(os.fdopen(fd, mode+"b", buffering=0), encoding="utf-8", write_through=True)
                        setattr(sys, name, stream)
    path = Path(script).resolve(strict=True)
    sys.argv = [str(path), *arguments]
    sys.path.insert(0, str(path.parent))
    runpy.run_path(str(path), run_name="__main__")


def appearance(root):
    root.configure(background=C["paper"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(FONT, 10), background=C["paper"], foreground=C["ink"])
    style.configure("TButton", padding=(12, 8))
    style.configure("Primary.TButton", background=C["teal"], foreground="white", borderwidth=0, padding=(18, 10))
    style.map("Primary.TButton", background=[("disabled", "#859B9C"), ("active", "#065F58")])
    style.configure("Card.TFrame", background=C["white"])
    style.configure("Card.TLabel", background=C["white"])
    style.configure("Sub.TLabel", foreground=C["muted"])
    style.configure("Title.TLabel", font=(FONT, 21, "bold"))
    style.configure("CardTitle.TLabel", font=(FONT, 13, "bold"), background=C["white"])
    style.configure("TEntry", fieldbackground="white", padding=5)
    style.configure("TCombobox", fieldbackground="white", padding=5)


def setup_screen():
    root = tk.Tk()
    root.title("安装 · 死神vs火影 AI 竞技场")
    appearance(root)
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)
    heading = "安装 AI 竞技场比赛版" if (RESOURCES / "Entrants").is_dir() else "安装 AI 竞技场"
    ttk.Label(frame, text=heading, style="Title.TLabel").pack(anchor="w")
    ttk.Label(frame, text=f"选择已解压的 Windows 竞技版 {BUILD} 游戏目录。", style="Sub.TLabel").pack(anchor="w", pady=(5, 14))
    selected = tk.StringVar(value=str(game_hint() or ""))
    field = ttk.Frame(frame)
    field.pack(fill="x")
    ttk.Entry(field, textvariable=selected, width=56).pack(side="left", expand=True, fill="x")
    def browse():
        picked = filedialog.askdirectory(parent=root, initialdir=selected.get() or None)
        if picked:
            selected.set(picked)
    ttk.Button(field, text="选择目录", command=browse).pack(side="left", padx=(8, 0))
    def install():
        try:
            if not FROZEN:
                raise ValueError("源码运行请先使用 build.ps1 构建 Windows 安装包。")
            exe = deploy(selected.get(), installer_manager(), RESOURCES)
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror("安装失败", str(e), parent=root)
            return
        if messagebox.askyesno("安装成功", f"比赛端已安装至\n{exe}\n\n现在打开比赛端？", parent=root):
            subprocess.Popen([str(exe)], cwd=exe.parent)
        root.destroy()
    def uninstall():
        if not messagebox.askyesno("卸载竞技场", "移除比赛端、插件和原样的示例文件？已保存的比赛、回放和修改过的示例将保留。", parent=root):
            return
        try:
            kept = remove_installation(selected.get())
        except (OSError, ValueError) as e:
            messagebox.showerror("卸载失败", str(e), parent=root)
            return
        messagebox.showinfo("已卸载", f"已移除竞技场组件。保留了 {len(kept)} 个改动过的组件文件。", parent=root)
    actions = ttk.Frame(frame)
    actions.pack(fill="x", pady=(22, 0))
    ttk.Button(actions, text="安装到此目录", style="Primary.TButton", command=install).pack(side="right")
    ttk.Button(actions, text="卸载已有安装", command=uninstall).pack(side="left")
    root.mainloop()


class Desktop:
    def __init__(self, root, game):
        self.root, self.game = root, Path(game)
        self.data = self.game / "AIArena/data"
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.running = self.closing = False
        self.replays = []
        self.replay_windows = []
        root.title("死神vs火影 · AI 竞技场")
        root.geometry("850x690")
        root.minsize(720, 610)
        appearance(root)
        main = ttk.Frame(root, padding=(24, 18))
        main.pack(fill="both", expand=True)
        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, text="AI 竞技场", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text=f"死神vs火影  /  竞技版 {BUILD}", style="Sub.TLabel").pack(side="right")
        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True, pady=(16, 0))
        setup = ttk.Frame(self.tabs, padding=16)
        library = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(setup, text="  比赛  ")
        self.tabs.add(library, text="  回放  ")
        modes = ttk.Frame(setup)
        modes.pack(fill="x", pady=(0, 12))
        ttk.Label(modes, text="比赛模式").pack(side="left", padx=(0, 8))
        self.match_mode = ttk.Combobox(modes, state="readonly", values=MATCH_MODES, width=27)
        self.match_mode.pack(side="left")
        self.match_mode.current(0)
        self.match_mode.bind("<<ComboboxSelected>>", self.change_match_mode)
        teams = ttk.Frame(setup)
        teams.pack(fill="x")
        teams.columnconfigure((0, 1), weight=1, uniform="team")
        self.controls = [self.match_mode]
        self.teams = []
        self.team_cards = []
        entrants = self.game / ENTRANTS
        bundled = sorted(p.parent for p in entrants.glob("*/entrant.json"))
        examples = self.game / "AIArena/SDK/Examples"
        if not examples.is_dir():
            examples = RESOURCES / "SDK/Examples"
        defaults = bundled or sorted(p.parent for p in examples.glob("*/entrant.json"))
        for index in range(2):
            card = ttk.Frame(teams, style="Card.TFrame", padding=12)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 5) if not index else (5, 0))
            self.team_cards.append(card)
            title = ttk.Label(card, text=("选手 A", "选手 B")[index], style="CardTitle.TLabel")
            title.pack(anchor="w")
            var = tk.StringVar(value=str(defaults[index]) if len(defaults) > index else "")
            self.teams.append(var)
            team_name = tk.StringVar()
            name_label = ttk.Label(card, textvariable=team_name, style="Card.TLabel")
            name_label.pack(anchor="w", pady=(5, 3))
            def update(*_, var=var, team_name=team_name):
                team_name.set(entrant_name(var.get()) or "请选择含 entrant.json 的文件夹")
            var.trace_add("write", update)
            update()
            field = ttk.Entry(card, textvariable=var)
            field.pack(fill="x")
            def pick(v=var):
                initial = str(entrants) if bundled else (v.get() or str(self.game))
                selection = filedialog.askdirectory(parent=root, title="选择选手文件夹", initialdir=initial)
                if selection:
                    v.set(selection)
            btn = ttk.Button(card, text="更换选手", command=pick)
            btn.pack(anchor="w", pady=(7, 0))
            self.controls.extend((field, btn))
            if index == 1:
                self.opponent_title, self.opponent_name = title, name_label
                self.opponent_field, self.opponent_browse = field, btn
                self.opponent_options = [str(path) for path in defaults]
                self.opponent_labels = [f"{entrant_name(path) or path.name}  ·  {path.name}" for path in defaults]
                self.opponent_box = ttk.Combobox(card, state="readonly", values=self.opponent_labels)
                self.opponent_box.bind("<<ComboboxSelected>>", self.choose_opponent)
                var.trace_add("write", self.sync_opponent)
                self.controls.append(self.opponent_box)
        self.human_card = ttk.Frame(teams, style="Card.TFrame", padding=12)
        self.human_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.human_card.grid_remove()
        ttk.Label(self.human_card, text="玩家 · P1", style="CardTitle.TLabel").pack(anchor="w")
        self.human_fighters, self.human_assists = list_human_choices(self.game)
        starters = default_roster([i for i, _ in self.human_fighters], 3)
        self.fighter_boxes = []
        for index, label in enumerate(("首发", "第 2 位", "第 3 位")):
            choice = starters[index] if index < len(starters) else None
            self.fighter_boxes.append(self.pick_box(label, self.human_fighters, choice))
        self.assist_box = self.pick_box("援助", self.human_assists, PREFERRED_ASSIST)
        options = ttk.Frame(setup)
        options.pack(fill="x", pady=(17, 13))
        ttk.Label(options, text="比赛地图").pack(side="left", padx=(0, 8))
        self.maps = list_maps(self.game)
        self.map_box = ttk.Combobox(options, state="readonly", width=27, values=[f"{label}  ·  {mid}" for mid, label in self.maps])
        self.map_box.pack(side="left")
        if self.maps:
            self.map_box.current(next((i for i, m in enumerate(self.maps) if m[0] == "gense"), 0))
        self.skip_cut_in = tk.BooleanVar()
        cut_in = ttk.Checkbutton(setup, text="跳过英雄和宠物的大招特写（实验）", variable=self.skip_cut_in)
        cut_in.pack(anchor="w", pady=(0, 10))
        self.controls.extend((self.map_box, cut_in))
        command = ttk.Frame(setup)
        command.pack(fill="x")
        self.start_button = ttk.Button(command, text="开始比赛", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(command, text="停止", command=self.stop.set, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.export_button = ttk.Button(command, text="导出 AI 训练包…", command=self.export)
        self.export_button.pack(side="right")
        self.controls.extend((self.start_button, self.export_button))
        self.status = tk.StringVar(value="七局四胜 · 小回合不限时 · 逐局换边")
        ttk.Label(setup, textvariable=self.status, wraplength=700).pack(anchor="w", pady=(13, 3))
        self.log = tk.Text(setup, height=7, wrap="word", font=(FONT, 9), padx=10, pady=8, bg="white", fg=C["ink"], borderwidth=0, state="disabled")
        self.log.pack(fill="both", expand=True)
        self.history = tk.Listbox(library, activestyle="none", font=(FONT, 11), background="white",
                                  borderwidth=0, selectbackground=C["teal"], selectmode=tk.EXTENDED,
                                  exportselection=False)
        self.history.pack(fill="both", expand=True)
        self.history.bind("<Double-Button-1>", lambda _: self.open_selected())
        self.history.bind("<Delete>", self.delete_selected)
        bar = ttk.Frame(library)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="播放所选回放", style="Primary.TButton", command=self.open_selected).pack(side="left")
        self.native_button = ttk.Button(bar, text="在游戏中重演（实验）", command=self.native_selected)
        self.native_button.pack(side="left", padx=(8, 0))
        self.controls.append(self.native_button)
        ttk.Button(bar, text="赛后复盘", command=self.report_selected).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开文件…", command=self.open_file).pack(side="left", padx=8)
        delete = ttk.Button(bar, text="删除所选…", command=self.delete_selected)
        delete.pack(side="left")
        self.controls.append(delete)
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="right")
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.update)

    def pick_box(self, label, choices, default):
        row = ttk.Frame(self.human_card, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text=label, width=6, style="Card.TLabel").pack(side="left")
        box = ttk.Combobox(row, state="readonly", values=[f"{name}  ·  {ident}" for ident, name in choices])
        box.pack(side="left", fill="x", expand=True)
        if choices and default is not None:
            box.current(next((i for i, (ident, _) in enumerate(choices) if ident == default), 0))
        self.controls.append(box)
        return box

    def choose_opponent(self, _event):
        index = self.opponent_box.current()
        if index >= 0:
            self.teams[1].set(self.opponent_options[index])

    def sync_opponent(self, *_):
        chosen = self.teams[1].get()
        if chosen in self.opponent_options:
            self.opponent_box.current(self.opponent_options.index(chosen))
        else:
            self.opponent_box.set(entrant_name(chosen) or "请选择选手文件夹")

    def change_match_mode(self, _event):
        mode = self.match_mode.current()
        if mode == 1:
            self.team_cards[0].grid_remove()
            self.human_card.grid()
            self.opponent_name.pack_forget()
            self.opponent_field.pack_forget()
            self.opponent_box.pack(fill="x", pady=(5, 0), before=self.opponent_browse)
            if not self.teams[1].get() and self.opponent_options:
                self.teams[1].set(self.opponent_options[0])
            self.sync_opponent()
            self.opponent_title.configure(text="AI 对手 · P2")
            self.status.set("单局 3 对 3 · 玩家在游戏窗口使用 P1 键位操作")
        else:
            self.human_card.grid_remove()
            self.team_cards[0].grid()
            self.opponent_box.pack_forget()
            self.opponent_field.pack(fill="x", before=self.opponent_browse)
            self.opponent_name.pack(anchor="w", pady=(5, 3), before=self.opponent_field)
            self.opponent_title.configure(text="选手 B")
            self.status.set("七局四胜 · 小回合不限时 · 逐局换边" if mode == 0 else "选手 A 对游戏 CPU · 单局")
        if mode == 2:
            self.team_cards[1].grid_remove()
        else:
            self.team_cards[1].grid()

    def refresh(self):
        self.replays = sorted(
            (p for p in self.data.rglob("*") if p.suffix == ".bvnreplay"
             or (p.suffix == ".bvndemo" and not p.with_suffix(".bvnreplay").is_file())),
            key=lambda p: p.stat().st_mtime, reverse=True)
        self.history.delete(0, tk.END)
        for path in self.replays:
            when = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
            self.history.insert(tk.END, f"{when}   {path.stem}   ·   {path.stat().st_size/1048576:.1f} MB")

    def open_file(self):
        path = filedialog.askopenfilename(parent=self.root, title="打开比赛回放",
                                         filetypes=[("比赛回放", ("*.bvnreplay", "*.bvndemo"))])
        if path:
            self.play(Path(path))

    def open_selected(self):
        selection = self.history.curselection()
        if selection:
            active = self.history.index(tk.ACTIVE)
            self.play(self.replays[active if active in selection else selection[0]])

    def report_selected(self):
        selection = self.history.curselection()
        if selection:
            from replay_viewer import open_battle_report
            active = self.history.index(tk.ACTIVE)
            open_battle_report(self.root, self.replays[active if active in selection else selection[0]])

    def delete_selected(self, _event=None):
        rows = self.history.curselection()
        if self.running or not rows:
            return
        from replay_files import files_for_selected_replays
        selected = [self.replays[i] for i in rows]
        try:
            files = files_for_selected_replays(self.data, selected)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法删除回放", str(exc), parent=self.root)
            return
        if not files:
            self.refresh()
            return
        self.replay_windows = [viewer for viewer in self.replay_windows if viewer.win.winfo_exists()]
        if any(viewer.reader.path in files for viewer in self.replay_windows):
            messagebox.showinfo("回放正在打开", "请关闭所选回放的播放窗口，再删除。", parent=self.root)
            return
        names = "\n".join(path.stem for path in selected[:5])
        if len(rows)>5:
            names += f"\n另有 {len(rows)-5} 场"
        if not messagebox.askyesno("删除选中的回放",
                                   f"将以下 {len(rows)} 场比赛的回放（共 {len(files)} 个文件）移到回收站？\n\n{names}",
                                   parent=self.root):
            return
        from send2trash import send2trash
        try:
            send2trash([str(path) for path in files])
        except OSError as exc:
            messagebox.showerror("无法删除部分回放", str(exc), parent=self.root)
        finally:
            self.refresh()

    def play(self, path):
        if path.suffix == ".bvndemo":
            self.native(path)
            return
        from replay_viewer import open_replay
        try:
            viewer = open_replay(self.root, path)
            if viewer is not None:
                self.replay_windows = [w for w in self.replay_windows if w.win.winfo_exists()]
                self.replay_windows.append(viewer)
        except Exception as exc:
            messagebox.showerror("无法打开回放", str(exc), parent=self.root)

    def native_selected(self):
        selection = self.history.curselection()
        if selection:
            active = self.history.index(tk.ACTIVE)
            path = self.replays[active if active in selection else selection[0]]
            companion = path.with_suffix(".bvndemo")
            self.native(companion if companion.is_file() else path)

    def native(self, path):
        if self.running:
            return
        from native_replay import play_native
        self.tabs.select(0)
        self.background(lambda:play_native(self.game, path, self.events.put, self.stop), True)

    def background(self, operation, stoppable):
        self.running = True
        self.stop.clear()
        for widget in self.controls:
            widget.state(["disabled"])
        if stoppable:
            self.stop_button.state(["!disabled"])
        def run():
            try:
                operation()
            except Exception as exc:
                self.events.put(str(exc))
            finally:
                self.events.put(None)
        threading.Thread(target=run, daemon=False).start()

    def start(self):
        from referee import run_human_match, run_series
        chosen = self.map_box.current()
        if chosen < 0:
            messagebox.showerror("地图", "请先选择地图。", parent=self.root)
            return
        a, b = (Path(v.get()) for v in self.teams)
        mode = self.match_mode.current()
        selected_entrants = ((a, b), (b,), (a,))[mode]
        if any(not (path / "entrant.json").is_file() for path in selected_entrants):
            messagebox.showerror("选手", "请选择含 entrant.json 的选手文件夹。", parent=self.root)
            return
        mid = self.maps[chosen][0]
        skip = self.skip_cut_in.get()
        if mode == 1:
            selected = [box.current() for box in self.fighter_boxes]
            selected_assist = self.assist_box.current()
            if -1 in selected or selected_assist < 0 or len(set(selected)) != 3:
                messagebox.showerror("玩家阵容", "请选择三位不同的出场角色和一位援助。", parent=self.root)
                return
            roster = [self.human_fighters[i][0] for i in selected]
            assist = self.human_assists[selected_assist][0]
            operation = lambda: run_human_match(self.game, b, mid, self.data, self.events.put, self.stop,
                                                roster=roster, assist=assist, skip_cut_in=skip)
        else:
            other = None if mode == 2 else b
            operation = lambda: run_series(self.game, a, other, mid, self.data, self.events.put, self.stop,
                                            skip_cut_in=skip)
        self.status.set("正在启动比赛…")
        self.background(operation, True)

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="导出含本地游戏的训练包", defaultextension=".zip",
                                            initialfile="BVN-AI-训练包.zip", filetypes=[("训练包", "*.zip")])
        if not path:
            return
        def job():
            self.events.put("正在打包本地游戏、比赛端和示例选手…")
            export_training_kit(self.game, path)
            self.events.put(f"训练包已保存到 {path}")
        self.background(job, False)

    def update(self):
        while not self.events.empty():
            value = self.events.get()
            if value is None:
                self.running = False
                for widget in self.controls:
                    widget.state(["!disabled"])
                self.stop_button.state(["disabled"])
                self.refresh()
                if self.closing:
                    self.root.destroy()
                    return
                continue
            self.status.set(str(value))
            self.log.configure(state="normal")
            self.log.insert(tk.END, f"{time.strftime('%H:%M:%S')}   {value}\n")
            self.log.see(tk.END)
            self.log.configure(state="disabled")
        self.root.after(100, self.update)

    def close(self):
        if self.running:
            if messagebox.askyesno("关闭比赛端", "结束当前任务后退出？正在进行的比赛会保留中断记录。", parent=self.root):
                self.closing = True
                self.stop.set()
        else:
            self.root.destroy()


def main():
    if sys.argv[1:2] == ["--entrant-script"] and len(sys.argv)>2:
        return bundled_script(sys.argv[2], sys.argv[3:])
    cli = argparse.ArgumentParser(description="Bleach vs Naruto AI Arena")
    action = cli.add_mutually_exclusive_group()
    action.add_argument("--install", metavar="GAME")
    action.add_argument("--uninstall", metavar="GAME")
    action.add_argument("--entrant-script", metavar="SCRIPT")
    action.add_argument("--game", metavar="GAME")
    action.add_argument("--replay", metavar="FILE")
    action.add_argument("--export", nargs=2, metavar=("GAME", "ZIP"))
    action.add_argument("--series", nargs=5, metavar=("GAME", "BOT_A", "BOT_B", "MAP", "DATA"))
    action.add_argument("--referee-api", nargs=2, metavar=("REQUEST", "RESPONSE"),
                        help="handle a local JSON referee request")
    cli.add_argument("--skip-super-cut-in", action="store_true")
    args = cli.parse_args()
    if args.entrant_script:
        bundled_script(args.entrant_script)
    elif args.install:
        if not FROZEN:
            raise ValueError("请使用编译好的安装程序。")
        deploy(args.install, installer_manager(), RESOURCES)
    elif args.uninstall:
        remove_installation(args.uninstall)
    elif args.export:
        export_training_kit(*args.export)
    elif args.referee_api:
        from referee_api import handle_request
        executable = Path(sys.executable)
        default_game = executable.parent.parent if FROZEN and executable.name.lower() == "bvnarena.exe" else None
        return handle_request(*args.referee_api, default_game=default_game)
    elif args.series:
        from referee import run_series
        game, team_a, team_b, map_id, data = args.series
        report = Path(data).resolve() / "run.log"
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8", buffering=1) as log:
            try:
                result = run_series(game, team_a, None if team_b == "-" else team_b, map_id, data,
                                    lambda message: print(message, file=log), threading.Event(),
                                    skip_cut_in=args.skip_super_cut_in)
                print(json.dumps(result, ensure_ascii=False), file=log)
            except Exception as exc:
                print(f"失败：{exc}", file=log)
                return 1
    elif args.replay:
        from replay_viewer import open_replay
        root = tk.Tk()
        root.withdraw()
        open_replay(root, Path(args.replay))
        def exit_when_closed():
            if not root.winfo_children():
                root.destroy()
            else:
                root.after(200, exit_when_closed)
        root.after(200, exit_when_closed)
        root.mainloop()
    elif args.game or (FROZEN and Path(sys.executable).name.lower() == "bvnarena.exe"):
        game = check_game(args.game or Path(sys.executable).parent.parent)
        root = tk.Tk()
        Desktop(root, game)
        root.mainloop()
    else:
        setup_screen()


if __name__ == "__main__":
    raise SystemExit(main())
