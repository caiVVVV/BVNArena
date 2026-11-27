# -*- coding: utf-8 -*-
"""豆包（Doubao）·《死神 vs 火影 竞技版》3.8.8.5 AI 选手 · 协议 1 · v2

逐行读 UTF-8 JSON、逐行回复并立即 flush；仅用裁判公开观测；仅标准库；
单拍决策 O(1)，远低于 40ms 行动窗口。

v2 基于真实对局回放（SQLite 事件流）校准的 mechanics：
- 决策间隔 dt=2（60fps 游戏），能量槽 0-100、开局满槽、每拍约 +1.1；
- 普攻有效距离 p90≈54，技能 p90≈76，步行速度≈2.12、冲刺≈6.08 (x/tick)；
- state 码：0 闲置/行走，10 普攻，11/12 技能，13 援助/特殊，15 冲刺(无敌)，
  20 格挡(挡普攻、有少量 chip、被技能/必杀破防)，21/22 受击硬直(约60tick)，
  23/24 倒地，40 必杀；
- 主动起跳仅在闲置态生效，空中约 48tick、上升约 70y；地面 y 随地图/层级变化，
  以 air 标志为准，缺失时用近期 y 窗口推断；
- 技能/必杀消耗因角色而异（实测 20-80），从能量骤降+状态转移在线学习。

风格「静水反击」：状态驱动优先级 + 距离分层滞回。
破防技→无敌冲刺穿透；普攻→格挡；对手硬直/倒地→长窗惩罚连段；
对手格挡→技能破防或冲刺穿背；能量满→硬直中必杀收尾；援助解围/扩大战果。
触发键上一拍按住则本拍强制松开，严格满足“再按前先松”的协议要求。
"""

import json
import random
import sys

ALLOWED = {"left", "right", "forward", "back", "up", "down",
           "attack", "jump", "dash", "skill", "super", "assist"}
TAP = ("attack", "jump", "dash", "skill", "super", "assist")
COOLDOWN = {"attack": 8, "dash": 24, "skill": 40, "super": 120,
            "assist": 600, "jump": 60}
DEFAULT_DT = 2
MAX_DT = 240

CLOSE = 58      # 普攻有效距离（实测 p90=54）
MID = 100       # 技能有效距离（实测 p90=76）
FAR = 205       # 远距阈值

STUN_STATES = (21, 22, 23, 24)
SKILL_STATES = (11, 12, 40, 13)

# 选角偏好（出场顺序，ID 来自裁判目录）；名单外自动回退
PREFERRED = ["ichigo", "sasuke", "naruto", "itachi", "kenpachi", "byakuya",
             "tensa", "ichigo_zero", "sasuke_susanoo", "kakashi_kamui",
             "naruto_kyubi", "madara", "pain", "ulquiorra", "grimmjow",
             "lee", "gaara", "rukia", "yoruichi", "soifon", "aizen_captain",
             "kakashi", "minato"]
ASSIST_PREFERRED = ["gaara", "minato", "itachi", "madara", "ichigo_dangai",
                    "gin", "toushirou", "temari", "yachiru", "soifon",
                    "harribel_resurrection", "starrk_resurrection", "nemu",
                    "riruka", "killerbee"]


def num(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def state_of(d):
    v = d.get("state")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def sanitize(keys):
    seen, out = set(), []
    for k in keys:
        if k in ALLOWED and k not in seen:
            seen.add(k)
            out.append(k)
    for a, b in (("left", "right"), ("forward", "back"), ("up", "down")):
        if a in seen and b in seen:
            out.remove(b)
            seen.discard(b)
    if (seen & {"left", "right"}) and (seen & {"forward", "back"}):
        out = [k for k in out if k not in ("left", "right")]
    return out


class Side:
    __slots__ = ("fid", "last_x", "last_hp", "last_en", "ys", "energy_max",
                 "skill_cost", "super_cost")

    def __init__(self, fid):
        self.fid = fid
        self.last_x = None
        self.last_hp = None
        self.last_en = None
        self.ys = []            # 近期 y（air 缺失时的地面推断窗口）
        self.energy_max = 0.0
        self.skill_cost = 30.0  # 在线学习前的保守默认
        self.super_cost = 80.0


class Bot:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.prev = []
        self.last_tick = None
        self.tick = 0
        self.dt = DEFAULT_DT
        self.me = None
        self.enemy = None
        self.enemy_vx = 0.0
        self.gap_last = None
        self.closing = 0.0
        self.camp = 0
        self.mode = "approach"
        self.mode_until = 0
        self.hurt_at = -10 ** 9
        self.hit_at = -10 ** 9
        self.cd = {k: -10 ** 9 for k in COOLDOWN}

    # ---------------- 选角 ----------------
    def draft(self, msg):
        pool = [f for f in (msg.get("fighters") or []) if isinstance(f, str)]
        pool = list(dict.fromkeys(pool))
        try:
            size = int(msg.get("teamSize"))
        except (TypeError, ValueError):
            size = 3
        size = max(1, size)
        order = [p for p in PREFERRED if p in pool]
        order += [p for p in pool if p not in order]
        roster = order[:size]
        assists = [a for a in (msg.get("assists") or []) if isinstance(a, str)]
        assist = next((a for a in ASSIST_PREFERRED if a in assists),
                      assists[0] if assists else "")
        return {"type": "pick", "roster": roster, "assist": assist}

    # ---------------- 战斗 ----------------
    def step(self, msg):
        try:
            tick = int(msg.get("tick"))
        except (TypeError, ValueError):
            tick = 0
        dt = DEFAULT_DT
        if self.last_tick is not None:
            d = tick - self.last_tick
            if 0 < d <= MAX_DT:
                dt = d
            elif d <= 0:
                self.cd = {k: -10 ** 9 for k in COOLDOWN}
                self.mode_until = 0
        self.tick, self.dt = tick, dt
        self.last_tick = tick

        players = msg.get("players") or []
        seat = msg.get("seat")
        if seat not in (1, 2) or len(players) < 2:
            self.prev = []
            return []
        raw_me = players[seat - 1]
        raw_en = players[2 - seat]
        if not isinstance(raw_me, dict):
            raw_me = {}
        if not isinstance(raw_en, dict):
            raw_en = {}

        # --- 己方 ---
        fid = raw_me.get("id") or "?"
        if self.me is None or self.me.fid != fid:
            self.me = Side(fid)
            self.mode, self.mode_until = "approach", 0
            self.cd["jump"] = -10 ** 9
            self.gap_last = None
            self.closing = 0.0
        me = self.me
        mx = num(raw_me, "x")
        if mx is None:
            mx = me.last_x if me.last_x is not None else 0.0
        me.last_x = mx
        my = num(raw_me, "y")
        if my is None:
            my = me.ys[-1] if me.ys else 0.0
        me.ys.append(my)
        if len(me.ys) > 8:
            me.ys.pop(0)
        air = raw_me.get("air")
        if isinstance(air, bool):
            grounded = not air
        else:
            grounded = my >= max(me.ys) - 30.0   # 跳跃上升约70，窗口内低于30视为离地
        st_me = state_of(raw_me)
        hp = num(raw_me, "hp")
        hpm = num(raw_me, "hpMax")
        my_ratio = hp / hpm if hp is not None and hpm and hpm > 0 else None
        if hp is not None and me.last_hp is not None and hp < me.last_hp - 0.5:
            self.hurt_at = tick
        me.last_hp = hp
        en = num(raw_me, "energy", "qi")
        if en is not None:
            if en > me.energy_max:
                me.energy_max = en
            if me.last_en is not None:
                drop = me.last_en - en
                if drop >= 15:
                    if st_me in (11, 12):
                        me.skill_cost = max(me.skill_cost, drop)
                    elif st_me == 40:
                        me.super_cost = max(me.super_cost, drop)
            me.last_en = en

        # --- 对手 ---
        eid = raw_en.get("id") or "?"
        if self.enemy is None or self.enemy.fid != eid:
            self.enemy = Side(eid)
            self.enemy_vx = 0.0
            self.gap_last = None
            self.closing = 0.0
        foe = self.enemy
        ex = num(raw_en, "x")
        if ex is None:
            ex = foe.last_x if foe.last_x is not None else mx + 300.0
        if foe.last_x is not None and dt > 0:
            v = (ex - foe.last_x) / dt
            self.enemy_vx = 0.6 * self.enemy_vx + 0.4 * v
        foe.last_x = ex
        eair = raw_en.get("air")
        enemy_air = bool(eair) if isinstance(eair, bool) else False
        st_en = state_of(raw_en)
        ehp = num(raw_en, "hp")
        ehpm = num(raw_en, "hpMax")
        en_ratio = ehp / ehpm if ehp is not None and ehpm and ehpm > 0 else None
        if ehp is not None and foe.last_hp is not None and ehp < foe.last_hp - 0.5:
            self.hit_at = tick
        foe.last_hp = ehp

        gap = abs(ex - mx)
        if self.gap_last is not None and dt > 0:
            self.closing = (self.gap_last - gap) / dt
        self.gap_last = gap
        if abs(self.enemy_vx) < 0.15 and gap > 140:
            self.camp += dt
        else:
            self.camp = max(0, self.camp - 2 * dt)

        keys = self.decide(gap, grounded, enemy_air, st_me, st_en,
                           my_ratio, en_ratio, en, me)
        keys = self.pulse(keys, grounded, st_me)
        keys = sanitize(keys)
        for k in keys:
            if k in COOLDOWN:
                self.cd[k] = tick + COOLDOWN[k]
        self.prev = keys
        return keys

    # ---------------- 决策 ----------------
    def decide(self, gap, grounded, enemy_air, st_me, st_en,
               my_ratio, en_ratio, en, me):
        t = self.tick
        hurt = t - self.hurt_at <= 8
        hit = t - self.hit_at <= 12
        foe_stun = st_en in STUN_STATES
        foe_skill = st_en in SKILL_STATES
        foe_attack = st_en == 10
        foe_guard = st_en == 20
        foe_dash = st_en == 15
        rush = (self.closing > 2.5 or foe_dash) and gap < 160
        can_act = st_me not in STUN_STATES
        can_jump = grounded and st_me in (0, None)
        base = me.energy_max if me.energy_max > 0 else 100.0
        if en is not None:
            skill_ok = en >= me.skill_cost
            super_ok = en >= me.super_cost
        else:
            skill_ok = t - self.cd["skill"] >= 120
            super_ok = (en_ratio is not None and en_ratio < 0.3
                        and t - self.cd["super"] >= 600)
        low_en = en_ratio is not None and en_ratio < 0.28
        low_me = my_ratio is not None and my_ratio < 0.25
        dash_ok = t >= self.cd["dash"]
        skill_ready = skill_ok and t >= self.cd["skill"]
        super_ready = super_ok and t >= self.cd["super"]
        assist_ready = t >= self.cd["assist"]

        # --- 空中：漂移接近 + 空中攻击 ---
        if not grounded:
            keys = ["forward"] if gap > 40 else []
            if gap < MID and can_act:
                keys.append("attack")
            return keys

        # --- 状态驱动优先反应 ---
        # 1) 对手破防技（技能/必杀/援助）：无敌冲刺穿透，冷却中则格挡/拉开
        if foe_skill and gap < FAR + 40 and can_act:
            if dash_ok:
                return ["forward", "dash"]
            return ["down"] if gap < 130 else ["back"]
        # 2) 对手硬直/倒地：长窗惩罚（硬直约60tick）
        if foe_stun and can_act:
            keys = ["forward"] if gap > CLOSE else []
            if gap > MID and dash_ok:
                keys.append("dash")
            keys.append("attack")
            if skill_ready and gap < 130:
                keys.append("skill")
            if super_ready and gap < 180 and st_en in (21, 22):
                keys = ["super"]
            elif assist_ready and gap < 200 and self.rng.random() < 0.6:
                keys.append("assist")
            return keys
        # 3) 对手普攻贴身：格挡（chip 远小于全额），援助解围
        if foe_attack and gap < CLOSE + 30 and can_act:
            if hurt and assist_ready:
                return ["assist"]
            return ["down"]
        # 4) 刚脱离受击：优先无敌冲刺脱离，否则格挡
        if hurt and st_me == 0 and can_act:
            if dash_ok and gap < 160:
                return ["forward", "dash"]
            if gap < CLOSE + 20:
                return ["down"]

        # --- 距离分层（带滞回）---
        if low_en and gap < 220:
            want = "finish"
        elif rush and gap < 130 and not hit:
            want = "defend"
        elif gap > FAR:
            want = "approach"
        elif gap > MID:
            want = "space"
        else:
            want = "pressure"
        urgent = want in ("defend", "finish")
        if want != self.mode and (urgent or t >= self.mode_until):
            self.mode = want
            self.mode_until = t + self.rng.randint(10, 22)
        mode = self.mode

        if mode == "approach":
            keys = ["forward"]
            if dash_ok and gap > 240:
                keys.append("dash")
        elif mode == "space":
            keys = ["forward"] if gap > 150 else (
                ["back"] if low_me and not hit else ["forward"])
            if foe_guard and skill_ready and gap < 150:
                keys.append("skill")            # 技能破格挡
            elif skill_ready and gap < 130 and (hit or st_en == 0):
                keys.append("skill")
            if dash_ok and gap > 150 and self.rng.random() < 0.06:
                keys.append("dash")
        elif mode == "pressure":
            keys = ["forward"] if gap > CLOSE else []
            if enemy_air and gap < CLOSE + 20:
                keys = ["attack"]               # 对空
            elif foe_guard:
                if skill_ready:
                    keys.append("skill")        # 破防
                elif dash_ok:
                    keys.append("dash")         # 穿背
                else:
                    keys.append("attack")       # chip
            elif gap < CLOSE and self.rng.random() < 0.15:
                keys = ["down", "attack"]       # 下段变化
            else:
                keys.append("attack")
            if skill_ready and hit and gap < MID:
                keys.append("skill")            # 连段收尾
            if super_ready and (foe_stun or low_me) and gap < 150:
                keys = ["super"]
            elif assist_ready and hit and gap < MID and self.rng.random() < 0.5:
                keys.append("assist")
        elif mode == "defend":
            if gap < CLOSE + 20:
                keys = ["down"]
                if hurt and assist_ready:
                    keys = ["assist"]
                elif dash_ok and self.rng.random() < 0.15:
                    keys = ["forward", "dash"]  # 无敌穿透脱离
            else:
                keys = ["back"]
                if skill_ready and rush:
                    keys.append("skill")
        else:  # finish
            if super_ready and gap < 180:
                keys = ["super"]
            else:
                keys = ["forward"] if gap > CLOSE else []
                if gap < CLOSE + 40:
                    keys.append("attack")
                if assist_ready and gap < 150:
                    keys.append("assist")

        # --- 跳跃切入：仅闲置态、地面、冷却完毕、中远距 ---
        if (can_jump and 150 < gap < 300 and t >= self.cd["jump"]
                and "jump" not in self.prev
                and (self.camp > 90 or self.rng.random() < 0.012
                     or (foe_skill and gap > 160))):
            keys = [k for k in keys if k in ("forward", "back")]
            if "forward" not in keys:
                keys.insert(0, "forward")
            keys.append("jump")
        return keys

    def pulse(self, keys, grounded, st_me):
        prev = set(self.prev)
        out = []
        for k in keys:
            if k in TAP and k in prev:
                continue                        # 上一拍按住→本拍强制松开
            if k == "jump" and not (grounded and st_me in (0, None)):
                continue                        # 非闲置态起跳会被游戏忽略
            if k in TAP and k != "jump" and st_me in STUN_STATES:
                continue                        # 受击/倒地中出招无效
            out.append(k)
        return out


def main():
    bot = None
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    for raw in inp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype == "draft":
            seed = str(msg.get("matchId") or "") + "|" + str(msg.get("seat") or "")
            bot = Bot(seed)
            resp = bot.draft(msg)
        elif mtype == "frame":
            if bot is None:
                bot = Bot("0")
            try:
                buttons = bot.step(msg)
            except Exception:
                buttons = []
            try:
                tick = int(msg.get("tick"))
            except (TypeError, ValueError):
                tick = 0
            resp = {"type": "action", "tick": tick, "buttons": buttons}
        else:
            continue
        resp["requestId"] = msg.get("requestId")
        data = json.dumps(resp, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        out.write(data)
        out.flush()


if __name__ == "__main__":
    main()