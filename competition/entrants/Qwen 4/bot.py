# -*- coding: utf-8 -*-
"""Qwen — 《死神 vs 火影 竞技版》3.8.8.5 AI 选手（协议 1）。

风格：自适应防守反击（counter-puncher）。
- 保持偏好间距，用对手近期动作（出招状态 + 接近速度）建立侵略度模型；
- 对手冒进时停击/打断/后撤，对手挥空时冲刺惩罚；
- 血量或场面占优时转入压制连段，血量告急时拉开空间并呼叫援助拆压；
- 能量按观测到的量程自适应管理，必杀留给近身惩罚与斩杀；
- 跳跃走完整状态机：地面单按 jump -> 下一帧松键 -> 落地锁定后再跳。

仅使用裁判发来的公开观测；不读屏、不访问其他进程。纯标准库。
每个进程只打一局：首条 draft，之后 frame，EOF 退出。
调试：在本文件夹放置 debug.on 文件才会把观测写入 bot_debug.jsonl（默认关闭）。
"""
import json
import os
import random
import sys
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 可调阈值（游戏帧 / 像素） ----------------
CLOSE = 105          # 近身连段距离
POKE = 175           # 中距离牵制外沿
FAR = 265            # 必须主动接近的距离
JUMP_IN_MAX = 235    # 允许跳入的最远距离
ASSIST_CD = 260      # 援助尝试间隔（tick）
SUPER_CD = 220       # 必杀尝试间隔
SKILL_CD = 70        # 技能间隔
DASH_CD = 46         # 冲刺间隔
LAND_LOCK = 12       # 落地后的起跳锁定
SWAP_CAUTION = 45    # 换人后的谨慎期

PULSE_KEYS = frozenset(("attack", "jump", "dash", "skill", "super", "assist"))
LEGAL = frozenset(("left", "right", "forward", "back", "up", "down",
                   "attack", "jump", "dash", "skill", "super", "assist"))

# 选角偏好：反击 / 牵制 / 控场型优先，其余按裁判名单顺序回退。
FIGHTER_PREF = [
    "itachi", "byakuya", "ulquiorra", "kakashi_kamui", "pain", "toushirou",
    "gaara", "temari", "sasuke_susanoo", "madara", "gin", "starrk", "aizen",
    "obito", "uryuu", "konan", "neji", "yoruichi", "soifon", "ichigo_zero",
    "naruto_kyubi", "kenpachi", "lee", "ichigo", "naruto", "sasuke",
]
# 援助偏好：控场 / 拆压 / 护身类优先（按名单中实际出现的 ID 匹配）。
ASSIST_HINTS = [
    "toushirou", "temari", "gaara", "shikamaru", "byakuya", "itachi",
    "yamamoto", "madara", "minato", "urahara", "tsunade", "kenpachi",
]


def num(v, default=0.0):
    """容错取数：None / 非数值一律回退默认值。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return default


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def pick(msg):
    """盲选：只依据本局 draft 的真实名单，不猜测对手选择。"""
    pool, seen = [], set()
    for f in msg.get("fighters") or []:
        if isinstance(f, str) and f not in seen:
            seen.add(f)
            pool.append(f)
    size = int(num(msg.get("teamSize"), 3))
    size = max(0, min(size, len(pool)))
    ranked = [f for f in FIGHTER_PREF if f in seen]
    ranked += [f for f in pool if f not in set(FIGHTER_PREF)]
    roster = ranked[:size]
    assists = [a for a in (msg.get("assists") or []) if isinstance(a, str)]
    out = {"type": "pick", "roster": roster}
    if assists:
        out["assist"] = next(
            (a for a in assists if any(h in a.lower() for h in ASSIST_HINTS)),
            assists[0],
        )
    return out


class Brain:
    def __init__(self):
        self.rng = random.Random(0xD0AB0)
        self.debug = os.path.exists(os.path.join(HERE, "debug.on"))
        self._dbg = None
        self.reset_match()

    def reset_match(self):
        self.last_tick = None
        self.prev_fx = None
        self.prev_dist = 0.0
        self.prev_foe_atk = False
        self.last_out = set()
        self.my_id = None
        self.foe_id = None
        self.was_air = False
        self.jump_phase = 0        # 0 可跳 / 1 已按待松 / 2 已松待落地
        self.jump_start = -10**9
        self.jump_setup = 0        # 起跳准备：松键回到可行动状态的截止 tick
        self.land_lock = 0
        self.last_assist = -10**9
        self.last_super = -10**9
        self.last_skill = -10**9
        self.last_dash = -10**9
        self.swap_until = 0
        self.punish_until = 0
        self.energy_max = 0.0
        self.foe_commit = 0
        self.foe_hist = deque(maxlen=6)
        self.combo = 0
        self.close = CLOSE + self.rng.randint(-8, 8)
        self.poke = POKE + self.rng.randint(-10, 12)

    # ---------------- 能量门限（量程自适应） ----------------
    def _super_ok(self, en):
        if en < 0:
            return False
        m = self.energy_max
        if m <= 0:
            return False
        if m <= 4:                      # 以“格”为量程
            return en >= m
        if m <= 120:                    # 百分制
            return en >= max(40.0, 0.6 * m)
        return en >= max(100.0, 0.4 * m)  # 大数值量程（如 0~300）

    def _want_jump(self, dist, foe_air, rush, dt, tick, jumpable):
        if self.jump_phase or tick < self.land_lock or foe_air or not jumpable:
            return False
        if not (95 <= dist <= JUMP_IN_MAX):
            return False
        if rush and dist < 130:         # 对手贴脸冒进时不原地起跳
            return False
        return self.rng.random() < 0.012 * dt

    # ---------------- 主决策 ----------------
    def fight(self, msg):
        tick = int(num(msg.get("tick"), (self.last_tick or 0) + 1))
        dt = 1 if self.last_tick is None else max(1, min(12, tick - self.last_tick))
        self.last_tick = tick
        players = msg.get("players") or []
        seat = int(num(msg.get("seat"), 1))
        if len(players) < 2 or seat not in (1, 2):
            self.last_out = set()
            return []
        me = players[seat - 1]
        foe = players[2 - seat]
        me = me if isinstance(me, dict) else {}
        foe = foe if isinstance(foe, dict) else {}

        mx, my = num(me.get("x")), num(me.get("y"))
        fx, fy = num(foe.get("x")), num(foe.get("y"))
        mhp, fhp = num(me.get("hp"), 1.0), num(foe.get("hp"), 1.0)
        mmax = max(1.0, num(me.get("hpMax"), max(mhp, 1.0)))
        fmax = max(1.0, num(foe.get("hpMax"), max(fhp, 1.0)))
        mr, fr = clamp(mhp / mmax, 0.0, 1.0), clamp(fhp / fmax, 0.0, 1.0)
        en = me.get("energy")
        if en is None:
            en = me.get("qi")
        en = num(en, -1.0)
        if en >= 0:
            self.energy_max = max(self.energy_max, en)
        air = me.get("air")
        air = air if isinstance(air, bool) else (my < -6)
        fair = foe.get("air")
        fair = fair if isinstance(fair, bool) else (fy < -6)
        ms = str(me.get("state") or "").lower()
        fs = str(foe.get("state") or "").lower()
        # 3.8.8.5 实测：state 为整数动作码，0/14/22 可起跳，21(受击)/23(硬直) 不可。
        ms_code = me.get("state")
        ms_code = ms_code if isinstance(ms_code, int) and not isinstance(ms_code, bool) else None
        jumpable = ms_code is None or ms_code in (0, 14, 22)
        me_hurt = any(k in ms for k in ("hit", "hurt", "stun", "launch", "knock"))
        foe_atk = any(k in fs for k in ("attack", "skill", "super", "dash", "atk"))
        foe_dead = fhp <= 0 or "dead" in fs or "ko" in fs
        dist = abs(fx - mx)

        # 换人 / 换对手：重置局部模型，换人后短暂谨慎。
        mid, fid = me.get("id"), foe.get("id")
        if self.my_id is not None and mid != self.my_id:
            self.swap_until = tick + SWAP_CAUTION
            self.combo = 0
            self.jump_phase = 0
            self.jump_setup = 0
        if self.foe_id is not None and fid != self.foe_id:
            self.foe_commit = 0
            self.foe_hist.clear()
            self.combo = 0
        self.my_id, self.foe_id = mid, fid

        # 对手侵略度：出招状态 + 接近速度，随时间衰减。
        closing = 0.0
        if self.prev_fx is not None:
            closing = (self.prev_dist - dist) / dt
            toward = (fx - self.prev_fx) / dt * (1.0 if mx >= fx else -1.0)
            self.foe_hist.append(toward)
            if foe_atk:
                self.foe_commit = min(6, self.foe_commit + 2)
            elif closing > 2.0:
                self.foe_commit = min(6, self.foe_commit + 1)
            else:
                self.foe_commit = max(0, self.foe_commit - 1)
            if self.prev_foe_atk and not foe_atk and dist < self.poke and not air:
                self.punish_until = tick + 12   # 挥空惩罚窗口
        self.prev_fx, self.prev_dist, self.prev_foe_atk = fx, dist, foe_atk
        rush = self.foe_commit >= 3

        # 跳跃状态机：落地检测与超时复位。
        if self.was_air and not air:
            self.jump_phase = 0
            self.land_lock = tick + LAND_LOCK
        if self.jump_phase and not air and tick - self.jump_start > 25:
            self.jump_phase = 0   # 起跳被打断或失败：超时复位
        self.was_air = air

        caution = tick < self.swap_until
        super_ok = self._super_ok(en) and tick - self.last_super > SUPER_CD
        skill_ok = (en < 0 or en >= 30) and tick - self.last_skill > SKILL_CD
        assist_ok = tick - self.last_assist > ASSIST_CD and not caution
        dash_ok = tick - self.last_dash > DASH_CD
        r = self.rng.random
        desired = []

        if foe_dead:
            self.combo = 0
        elif me_hurt:
            pass                                  # 受击硬直：松键等待
        elif air:
            desired = ["forward"] if dist > 80 else ["attack"]
        elif self.jump_phase in (1, 2):
            # 起跳后先松 jump，并等待离地：期间只漂移、不出招，
            # 否则攻击/技能输入会取消跳跃前摇，导致永远跳不起来。
            desired = ["forward"] if dist > 60 else []
        else:
            # 起跳准备：当前状态不可行动时先松键回到 idle，再跳。
            setup_active = False
            if self.jump_setup:
                if tick >= self.jump_setup:
                    self.jump_setup = 0
                elif jumpable:
                    desired = ["forward", "jump"]
                    self.jump_start = tick
                    self.jump_setup = 0
                else:
                    desired = []
                    setup_active = True
            if not setup_active:
                if dist <= self.close:
                    # 近身：连段节奏 + 技能收尾 + 必杀斩杀 + 援助锁压。
                    self.combo += 1
                    if super_ok and not caution and (fr < 0.45 or mr - fr > 0.2):
                        desired = ["super"]
                        self.last_super = tick
                        self.combo = 0
                    elif en < 0 and not caution and tick - self.last_super > SUPER_CD and r() < 0.01:
                        desired = ["super"]           # 能量字段缺失时的低频盲试
                        self.last_super = tick
                    elif self.combo >= 3 and skill_ok:
                        desired = ["down", "skill"] if r() < 0.4 else ["skill"]
                        self.last_skill = tick
                        self.combo = 0
                    elif assist_ok and r() < (0.3 if rush else 0.15):
                        desired = ["assist"]
                        self.last_assist = tick
                    elif r() < 0.18:
                        desired = ["down", "attack"]  # 下段混合
                    elif r() < 0.42:
                        desired = ["forward", "attack"]
                    else:
                        desired = ["attack"]
                    if mr < 0.25 and rush and r() < 0.35:
                        desired = ["back", "dash"] if dash_ok else ["back"]
                        if dash_ok:
                            self.last_dash = tick
                elif dist <= self.poke:
                    # 中距牵制：诱空、停击、惩罚、控间距。
                    if rush and foe_atk:
                        desired = ["back"]
                        if assist_ok and r() < 0.3:
                            desired = ["assist"]
                            self.last_assist = tick
                    elif tick < self.punish_until:
                        desired = ["forward", "dash"] if dash_ok else ["forward"]
                        if dash_ok:
                            self.last_dash = tick
                    elif foe_atk and closing > 1.5:
                        desired = ["back"]
                    elif dist > 140:
                        desired = ["forward"]
                    elif dist < 118:
                        desired = ["back"]
                    elif skill_ok and dist > 120 and r() < 0.06:
                        desired = ["skill"]
                        self.last_skill = tick
                    else:
                        desired = ["attack"] if r() < 0.3 else []
                    if not caution and self._want_jump(dist, fair, rush, dt, tick, jumpable):
                        desired = ["forward", "jump"]
                        self.jump_start = tick
                    elif not caution and not self.jump_phase and 95 <= dist <= JUMP_IN_MAX and not fair and r() < 0.01 * dt:
                        self.jump_setup = tick + 12
                        desired = []
                elif dist <= FAR:
                    if foe_atk and closing > 1.8:
                        desired = []                  # 停步诱空
                    else:
                        desired = ["forward"]
                        if dash_ok and r() < 0.25:
                            desired = ["forward", "dash"]
                            self.last_dash = tick
                    if tick < self.punish_until and dash_ok:
                        desired = ["forward", "dash"]
                        self.last_dash = tick
                    if not caution and self._want_jump(dist, fair, rush, dt, tick, jumpable):
                        desired = ["forward", "jump"]
                        self.jump_start = tick
                    elif not caution and not self.jump_phase and 95 <= dist <= JUMP_IN_MAX and not fair and r() < 0.01 * dt:
                        self.jump_setup = tick + 12
                        desired = []
                else:
                    desired = ["forward"]
                    if dash_ok and r() < 0.5:
                        desired = ["forward", "dash"]
                        self.last_dash = tick
                    if not caution and self._want_jump(dist, fair, rush, dt, tick, jumpable):
                        desired = ["forward", "jump"]
                        self.jump_start = tick
                    elif not caution and not self.jump_phase and 95 <= dist <= JUMP_IN_MAX and not fair and r() < 0.01 * dt:
                        self.jump_setup = tick + 12
                        desired = []

                # 对空：对手离地逼近时准备迎击 / 抓落地。
                if fair and dist < 150 and not (set(desired) & {"jump", "super", "assist"}):
                    desired = ["attack"] if dist < 115 else ["forward", "attack"]
                # 残血拆压：拉开空间、援助打断、必要时穿越跳。
                if mr < 0.28 and rush and dist <= self.poke and "jump" not in desired:
                    if dist < 65 and jumpable and r() < 0.08 and tick >= self.land_lock and not self.jump_phase:
                        desired = ["forward", "jump"]
                        self.jump_start = tick
                    else:
                        desired = ["back", "dash"] if dash_ok else ["back"]
                        if dash_ok:
                            self.last_dash = tick
                        if assist_ok and dist < 140 and r() < 0.4:
                            desired = ["assist"]
                            self.last_assist = tick

        if self.jump_phase == 1:
            desired = [k for k in desired if k != "jump"]
        return self._emit(desired)

    # ---------------- 输出整形：方向互斥 + 脉冲松键 ----------------
    def _emit(self, desired):
        want, seen = [], set()
        for k in desired:
            if k in LEGAL and k not in seen:
                seen.add(k)
                want.append(k)
        if "forward" in seen and "back" in seen:
            want.remove("back")
            seen.discard("back")
        if (seen & {"left", "right"}) and (seen & {"forward", "back"}):
            want = [k for k in want if k not in ("left", "right")]
            seen -= {"left", "right"}
        if "left" in seen and "right" in seen:
            want.remove("right")
            seen.discard("right")
        if "up" in seen and "down" in seen:
            want.remove("down")
            seen.discard("down")
        # 离散键不得连续两帧按住：要再按，先松一帧。
        out = [k for k in want if not (k in PULSE_KEYS and k in self.last_out)]
        if "jump" in out:
            self.jump_phase = 1
        elif self.jump_phase == 1:
            self.jump_phase = 2
        self.last_out = set(out)
        return out

    # ---------------- 可选调试（仅保存裁判发来的消息） ----------------
    def log(self, record):
        if not self.debug:
            return
        try:
            if self._dbg is None:
                self._dbg = open(os.path.join(HERE, "bot_debug.jsonl"), "a", encoding="utf-8")
            self._dbg.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
            self._dbg.flush()
        except Exception:
            self.debug = False

    def log_frame(self, msg, buttons):
        if not self.debug:
            return
        seat = int(num(msg.get("seat"), 1))
        players = msg.get("players") or []
        me, foe = {}, {}
        if seat in (1, 2) and len(players) >= 2:
            a, b = players[seat - 1], players[2 - seat]
            me = a if isinstance(a, dict) else {}
            foe = b if isinstance(b, dict) else {}
        keep = ("id", "x", "y", "hp", "hpMax", "energy", "qi", "air", "state")
        self.log({"tick": msg.get("tick"), "seat": seat,
                  "me": {k: me.get(k) for k in keep},
                  "foe": {k: foe.get(k) for k in keep},
                  "buttons": buttons})


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    brain = Brain()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue                      # 非法输入行：跳过，不污染协议
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        if kind == "draft":
            brain.reset_match()
            resp = pick(msg)
            brain.log({"event": "draft", "seat": msg.get("seat"), "roster": resp.get("roster"),
                       "assist": resp.get("assist")})
        elif kind == "frame":
            buttons = brain.fight(msg)
            resp = {"type": "action", "tick": brain.last_tick, "buttons": buttons}
            brain.log_frame(msg, buttons)
        else:
            continue
        resp["requestId"] = msg.get("requestId")
        try:
            sys.stdout.write(json.dumps(resp, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            return


if __name__ == "__main__":
    main()