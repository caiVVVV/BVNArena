#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek Flash —— 《死神 vs 火影 竞技版》协议 1 参赛选手。

只用 Python 标准库。从标准输入逐行读裁判消息，向标准输出逐行回复并立即刷新。

自测：同目录 selftest.py（python -X utf8 selftest.py）。

设计要点
--------
* 只使用裁判发给本进程的公开观测，不读屏、不碰其它进程。
* 观测字段缺失时沿用上一次已知值；帧号跳变/回退时仍按时钟推进，并重置短期记忆。
* 按键分两类：方向持续按住；动作（攻击/跳跃/冲刺/技能/必杀/援助）短按，
  每次按下后强制留出松开间隔，避免“一直按住导致不再触发”。
* 按游戏机制理解按键：down 是防御/蹲（S/↓），down+dash 是幽步脱离，
  attack+jump 等组合是派生技，skill（U）基本不耗气，super（I）耗一格气，
  援助（O）按冷却使用，受击瞬间按援助可尝试灵替/灵爆打断连段。
* 决策是距离分层 + 反应式状态机：命中确认追击、挨打防御/幽步/灵替脱离、防空、
  对手出招应对、对手僵直惩罚、劣势拉开距离、优势压制。不是固定循环。
"""

import json
import math
import os
import random
import sys

MAX_REPLY_BYTES = 64 * 1024
DEFAULT_HP_MAX = 700.0
DEFAULT_BAR_PEAK = 100.0

# 合法按键（协议 1）。本项目只用相对方向 forward/back，自动跟随朝向。
BUTTONS = frozenset((
    "left", "right", "forward", "back", "up", "down",
    "attack", "jump", "dash", "skill", "super", "assist",
))
HORIZONTAL = frozenset(("left", "right", "forward", "back"))
VERTICAL = frozenset(("up", "down"))

# 动作键两次按下之间至少间隔的时钟帧数（同时保证中间有一帧松开）。
PULSE_GAP = {
    "attack": 5,
    "jump": 24,
    "dash": 22,      # 瞬步耗耐力，避免狂按
    "skill": 50,     # 技能基本不耗气，按技能自身冷却节奏使用
    "super": 240,    # 必杀耗一格气，留出收招时间
    "assist": 900,   # 援助是冷却制，约十几秒一次
}

# 角色偏好：越靠前越先出场。
# 实测裁判 draft 会给出全部 55 个角色（含神阶/变身形态），可以自由选择。
# 但通用决策策略下，基础强角的输出反而更稳定（对 AI 正式赛里 aizen 首发平均
# 43 伤害/击，基础角约 55~61 伤害/击），所以首发用已验证的基础强角，神阶/变身作后备。
CHARACTER_PREFERENCE = (
    # 已验证稳定的基础强角（首发）
    "byakuya", "itachi", "kakashi", "sasuke", "ichigo",
    "gaara", "temari", "uryuu", "toushirou", "kenpachi",
    "lee", "rukia", "sakura", "soifon", "yoruichi",
    "renji", "ikkaku", "kenshin", "neji", "hinata", "kabuto",
    "kimimaro", "juggo", "chad", "orihime", "orochimaru",
    "deidara", "dordoni", "gin", "grimmjow", "jiraiya",
    "konan", "mayuri", "suigetsu",
    # 神阶 / 变身形态（可自由选择，作为后备或针对性安排）
    "aizen", "madara", "pain", "obito", "tobi", "aizen_captain",
    "ichigo_zero", "naruto_kyubi", "sasuke_susanoo", "tensa", "ogihci",
    "sado_muerte", "kakashi_kamui", "kakashi_gsw", "sakura_byakugo",
    "kimimaro_goninshu", "killerbee", "onoki", "ulquiorra", "starrk",
)

# 援助偏好：实测同样可自由选择，按泛用性与强度排序。
ASSIST_PREFERENCE = (
    "aizen_muken", "madara", "yamamoto", "ichigo_dangai",
    "sasuke_amenotejikara", "shunsui", "urahara", "minato",
    "starrk_resurrection", "harribel_resurrection", "ogihci_vasto_lord",
    "ikkaku_bankai", "byakuya", "toushirou", "itachi", "gaara", "temari",
    "uryuu", "onoki", "kenpachi", "tsunade", "orihime", "sakura_byakugo",
    "naruto", "nel", "yoruichi", "soifon", "renji", "utakata",
    "jiraiya", "kabuto", "kimimaro", "riruka", "shishio", "kensei",
    "hiyori", "rangiku", "shikamaru", "tenten", "kon", "bonnie",
    "choji", "fukasaku", "hiruko", "ino", "isane", "karin", "kira",
    "nemu", "sai", "tayuya", "yachiru", "zetsu", "suigetsu_taka",
)

# 状态字段的关键词归类（字符串 state，按关键词猜）。
STATE_STUN_WORDS = (
    "hurt", "hitstun", "stun", "stagger", "knock", "down", "falling",
    "fall", "damage", "dead", "dizzy", "ko", "bind", "freeze", "trapped",
)
STATE_BLOCK_WORDS = ("block", "guard", "defend", "shield", "parry")
STATE_ATTACK_WORDS = (
    "attack", "atk", "skill", "super", "combo", "slash", "strike",
    "punch", "kick", "cast", "special", "charge", "dash_attack", "air_attack",
)

# 原版游戏用数字 state（竞技版 3.8.8.5 实测枚举）：
#   0 待机 / 10 普攻 / 11 技能 / 12 必杀 / 15 冲刺 / 20 未知(援助/防御类) /
#   21 受击(地面) / 22 浮空受击 / 23 倒地 / 24 空中受击 / 40 收招硬直
STATE_INT_ATTACKING = frozenset((10, 11, 12))
STATE_INT_STUNNED = frozenset((21, 22, 24))
STATE_INT_BLOCKING = frozenset((20,))
STATE_INT_DOWN = frozenset((23,))
STATE_INT_RECOVERY = frozenset((40,))

# 资源条刻度提示：实测 qi 为 0..300（最多三格气，一格 100），energy 为耐力 0..100。
BAR_SCALE_HINTS = {"qi": 300.0, "energy": 100.0}


def as_number(value):
    """宽松取数：只接受有限数字或数字字符串，其它（含 None/bool）返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = value
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (ValueError, OverflowError):
            return None
    else:
        return None
    try:
        number = float(number)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def json_safe(value):
    """保证写进回复的值是合法 JSON；非有限浮点等异常值退化成字符串。"""
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def as_text(value):
    if isinstance(value, str):
        return value
    return None


def classify_state(value):
    """把 state 归类成 stunned/attacking/recovering/down/blocking/''。

    字符串 state 按关键词猜；数字 state 按实测枚举归类；缺字段返回 ''。
    """
    if isinstance(value, str):
        low = value.lower()
        if any(word in low for word in STATE_STUN_WORDS):
            return "stunned"
        if any(word in low for word in STATE_BLOCK_WORDS):
            return "blocking"
        if any(word in low for word in STATE_ATTACK_WORDS):
            return "attacking"
        number = as_number(low)
    else:
        number = as_number(value)
    if number is None:
        return ""
    code = int(number)
    if code in STATE_INT_STUNNED:
        return "stunned"
    if code in STATE_INT_ATTACKING:
        return "attacking"
    if code in STATE_INT_BLOCKING:
        return "blocking"
    if code in STATE_INT_RECOVERY:
        return "recovering"
    if code in STATE_INT_DOWN:
        return "down"
    return ""


def debug_enabled():
    return bool(os.environ.get("DVF_LOG"))


class CombatAI(object):
    """协议 1 的选角与战斗决策。"""

    def __init__(self):
        self.seat = 1
        self.match_id = ""
        self.rng = random.Random(0x5EED)

        # 时钟：用裁判的 tick 增量推进；tick 缺失时按每次决策一帧推进。
        self.clock = 0
        self.dt = 1
        self.decisions = 0
        self.tick = None
        self.last_tick = None

        # 观测记忆：每席一个字典，缺字段沿用上次值。
        self.known = {1: {}, 2: {}}
        self.previous = {1: {}, 2: {}}
        self.hp_max_hint = {1: DEFAULT_HP_MAX, 2: DEFAULT_HP_MAX}
        # 资源条观测到的峰值，用来自适应 0..1 / 0..3 / 0..10 / 0..100 等刻度。
        self.bar_peak = {
            1: {"energy": 0.0, "qi": 0.0},
            2: {"energy": 0.0, "qi": 0.0},
        }
        self.ground_y = {1: None, 2: None}
        self.air_run = {1: 0, 2: 0}

        # 本局/本小回合的短期记忆
        self.my_fighter = None
        self.foe_fighter = None
        self.last_hit_dealt_clock = -10 ** 9
        self.last_hit_taken_clock = -10 ** 9
        self.last_gap = None
        self.closing = 0.0

        # 场地边缘的弱估计（只作压制倾向的轻微修正）
        self.wall_left = None
        self.wall_right = None
        self.wall_left_seen = 0
        self.wall_right_seen = 0

        # 按键状态
        self.cooldown = {}
        self.last_buttons = []
        self.guard_streak = 0

        # 调试统计（只有 DVF_LOG 打开时才写文件）
        self.log_handle = None
        if debug_enabled():
            try:
                self.log_handle = open("deepseek-flash.log", "a", encoding="utf-8")
            except OSError:
                self.log_handle = None

    # ------------------------------------------------------------------ 协议

    def on_draft(self, msg):
        """处理选角：盲选，按偏好挑不重复的 roster 和一个援助。"""
        self.match_id = as_text(msg.get("matchId")) or ""
        seat = as_number(msg.get("seat"))
        self.seat = 2 if seat == 2 else 1
        seed = 0x5EED
        for ch in self.match_id:
            seed = (seed * 131 + ord(ch)) & 0xFFFFFFFF
        self.rng = random.Random(seed ^ (self.seat * 0x9E3779B1))

        fighters = self._id_list(msg.get("fighters"))
        assists = self._id_list(msg.get("assists"))
        team_size = self._team_size(msg.get("teamSize"), fighters)
        roster = self._choose_roster(fighters, team_size)

        self._log("draft_in", {
            "seat": self.seat,
            "teamSize": team_size,
            "fighters": fighters,
            "assists": assists,
            "map": msg.get("map"),
        })
        reply = {"type": "pick", "requestId": json_safe(msg.get("requestId")), "roster": roster}
        assist = self._choose_assist(assists)
        if assist is not None:
            reply["assist"] = assist
        self._log("draft", {"reply": reply})
        self.known = {1: {}, 2: {}}
        self.previous = {1: {}, 2: {}}
        self.bar_peak = {1: {"energy": 0.0, "qi": 0.0}, 2: {"energy": 0.0, "qi": 0.0}}
        self.ground_y = {1: None, 2: None}
        self.air_run = {1: 0, 2: 0}
        self.wall_left = None
        self.wall_right = None
        self.wall_left_seen = 0
        self.wall_right_seen = 0
        self.my_fighter = None
        self.foe_fighter = None
        self.last_hit_dealt_clock = -10 ** 9
        self.last_hit_taken_clock = -10 ** 9
        self.last_gap = None
        self.closing = 0.0
        self.cooldown = {}
        self.last_buttons = []
        self.guard_streak = 0
        return reply

    def on_frame(self, msg):
        """处理一帧观测：更新状态并给出这一窗口的按键。"""
        self.decisions += 1
        tick = self._tick_value(msg.get("tick"))
        dt = 1
        if tick is not None:
            if self.last_tick is not None:
                delta = tick - self.last_tick
                if delta < 0:
                    # 帧号回退：多半是新小回合/新对局，清掉短期记忆。
                    self._reset_short_term("tick_reset")
                    dt = 1
                elif delta == 0:
                    dt = 1
                else:
                    dt = min(delta, 12)
            self.last_tick = tick
            self.tick = tick
        self.dt = dt
        self.clock += dt

        self._absorb(msg.get("players"))
        self._track_geometry()
        self._handle_identity()
        self._update_reactions()

        buttons = self._decide()
        buttons = self._sanitize(buttons)
        if buttons == ["down"]:
            self.guard_streak += 1
        else:
            self.guard_streak = 0
        self.last_buttons = list(buttons)
        self._remember()

        reply = {
            "type": "action",
            "requestId": json_safe(msg.get("requestId")),
            "tick": self.tick if self.tick is not None else 0,
            "buttons": buttons,
        }
        self._log("frame", {
            "tick": self.tick,
            "seat": self.seat,
            "known": {str(s): dict(v) for s, v in self.known.items()},
            "buttons": buttons,
        })
        return reply

    def safe_reply(self, msg):
        """决策意外出错时的兜底回复：仍回传正确的 type/requestId/tick。"""
        kind = msg.get("type")
        if kind == "draft":
            fighters = self._id_list(msg.get("fighters"))
            team_size = self._team_size(msg.get("teamSize"), fighters)
            roster = self._choose_roster(fighters, team_size)
            assist = self._choose_assist(self._id_list(msg.get("assists")))
            reply = {"type": "pick", "requestId": json_safe(msg.get("requestId")), "roster": roster}
            if assist is not None:
                reply["assist"] = assist
            return reply
        if kind == "frame":
            tick = self._tick_value(msg.get("tick"))
            if tick is None:
                tick = self.tick if self.tick is not None else 0
            return {
                "type": "action",
                "requestId": json_safe(msg.get("requestId")),
                "tick": tick,
                "buttons": ["back"],
            }
        return None

    # -------------------------------------------------------------- 选角实现

    @staticmethod
    def _id_list(value):
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if isinstance(item, str) and item:
                result.append(item)
        return result

    @staticmethod
    def _team_size(value, fighters):
        unique = []
        for name in fighters:
            if name not in unique:
                unique.append(name)
        number = as_number(value)
        if number is None or number < 1:
            number = 3 if len(unique) >= 3 else max(1, len(unique))
        size = int(number)
        if unique:
            size = min(size, len(unique))
        return max(1, size)

    @staticmethod
    def _choose_roster(fighters, team_size):
        pool = []
        for name in fighters:
            if name not in pool:
                pool.append(name)
        ordered = [name for name in CHARACTER_PREFERENCE if name in pool]
        ordered.extend(name for name in pool if name not in ordered)
        roster = ordered[:team_size]
        # 名单比 teamSize 还短时只能尽力凑够长度（正常裁判不会这样发）。
        while len(roster) < team_size and ordered:
            roster.append(ordered[len(roster) % len(ordered)])
        return roster

    @staticmethod
    def _choose_assist(assists):
        # 有公开偏好时按偏好选第一个可用的；否则取首个非调试用援助。
        for name in ASSIST_PREFERENCE:
            if name in assists:
                return name
        for name in assists:
            if name != "debug":
                return name
        return assists[0] if assists else None

    # -------------------------------------------------------------- 观测更新

    @staticmethod
    def _tick_value(value):
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            # 整数帧号原样保留，避免 float 舍入。
            return value if -2 ** 63 <= value <= 2 ** 63 - 1 else None
        number = as_number(value)
        if number is None or abs(number) > 2.0 ** 53:
            return None
        return int(number)

    def _absorb(self, players):
        """把本帧观测合并进记忆；缺字段保留旧值。players 异常时不动。"""
        self.previous = {1: dict(self.known[1]), 2: dict(self.known[2])}
        if not isinstance(players, list) or len(players) < 2:
            return
        for seat in (1, 2):
            incoming = players[seat - 1]
            if not isinstance(incoming, dict):
                continue
            memory = dict(self.known[seat])

            fighter = as_text(incoming.get("id"))
            if fighter is not None:
                memory["id"] = fighter
            for field in ("x", "y", "hp", "hpMax", "energy", "qi", "facing", "state", "air"):
                if field not in incoming:
                    continue
                if field in ("x", "y", "hp", "hpMax", "energy", "qi"):
                    number = as_number(incoming.get(field))
                    if number is not None:
                        memory[field] = number
                else:
                    value = incoming.get(field)
                    if value is not None:
                        memory[field] = value
            self.known[seat] = memory

            if "hpMax" in memory and memory["hpMax"]:
                self.hp_max_hint[seat] = memory["hpMax"]
            for field in ("energy", "qi"):
                value = memory.get(field)
                if value is not None and value > self.bar_peak[seat][field]:
                    self.bar_peak[seat][field] = value

    def _track_geometry(self):
        """用双方横向位置估计场地左右边缘（只作弱证据）。"""
        for seat in (1, 2):
            x = self.known[seat].get("x")
            if x is None:
                continue
            if self.wall_left is None or x < self.wall_left - 1:
                self.wall_left = x
                self.wall_left_seen = 1
            elif abs(x - self.wall_left) <= 3:
                self.wall_left_seen += 1
            if self.wall_right is None or x > self.wall_right + 1:
                self.wall_right = x
                self.wall_right_seen = 1
            elif abs(x - self.wall_right) <= 3:
                self.wall_right_seen += 1

    def _handle_identity(self):
        mine = self.known[self.seat].get("id")
        theirs = self.known[3 - self.seat].get("id")
        if mine != self.my_fighter:
            if self.my_fighter is not None:
                self._reset_short_term("my_fighter_change")
                self.bar_peak[self.seat] = {"energy": 0.0, "qi": 0.0}
            self.my_fighter = mine
        if theirs != self.foe_fighter:
            if self.foe_fighter is not None:
                self._reset_short_term("foe_fighter_change")
                self.bar_peak[3 - self.seat] = {"energy": 0.0, "qi": 0.0}
            self.foe_fighter = theirs

    def _update_reactions(self):
        """用生命值变化判断“我们刚打中”与“我们刚挨打”。"""
        for seat in (1, 2):
            now = self.known[seat].get("hp")
            before = self.previous[seat].get("hp")
            if now is None or before is None:
                continue
            drop = before - now
            if drop <= 0.5:
                continue
            if seat == self.seat:
                self.last_hit_taken_clock = self.clock
            else:
                self.last_hit_dealt_clock = self.clock

        me_x = self.known[self.seat].get("x")
        foe_x = self.known[3 - self.seat].get("x")
        gap = None
        if me_x is not None and foe_x is not None:
            gap = abs(foe_x - me_x)
            if self.last_gap is not None and gap != self.last_gap:
                self.closing = (self.last_gap - gap) / max(self.dt, 1)
        self.last_gap = gap

    def _reset_short_term(self, reason):
        self.last_hit_dealt_clock = -10 ** 9
        self.last_hit_taken_clock = -10 ** 9
        self.last_gap = None
        self.closing = 0.0
        self.guard_streak = 0

    # -------------------------------------------------------------- 观测量

    def _gap(self):
        me_x = self.known[self.seat].get("x")
        foe_x = self.known[3 - self.seat].get("x")
        if me_x is None or foe_x is None:
            return None
        return abs(foe_x - me_x)

    def _hp_ratio(self, seat):
        hp = self.known[seat].get("hp")
        if hp is None:
            return None
        cap = self.known[seat].get("hpMax") or self.hp_max_hint[seat] or DEFAULT_HP_MAX
        if cap <= 0:
            cap = DEFAULT_HP_MAX
        return max(0.0, min(1.0, hp / cap))

    @staticmethod
    @staticmethod
    def _resource_scale(peak, field):
        """按观测峰值猜测资源条满值刻度；无观测返回 None。

        实测本作 qi 满值 300（三格气），energy（耐力）满值 100；
        其它刻度（0..1 / 0..3 / 0..10 / 归一化）也能自适应。
        """
        if peak is None or peak <= 0:
            return None
        if peak <= 1.05:
            return 1.0          # 0..1 归一化
        if peak <= 3.5:
            return 3.0          # 0..3 格
        if peak <= 10.5:
            return 10.0         # 0..10 刻度
        return max(peak, BAR_SCALE_HINTS.get(field, DEFAULT_BAR_PEAK))

    def _bar_ratio(self, seat, field):
        value = self.known[seat].get(field)
        if value is None:
            return None
        scale = self._resource_scale(self.bar_peak[seat].get(field), field)
        if scale is None or scale <= 0:
            return None
        return max(0.0, min(1.0, value / scale))

    def _airborne(self, seat):
        memory = self.known[seat]
        air = memory.get("air")
        if isinstance(air, bool):
            return air
        if isinstance(air, str):
            low = air.strip().lower()
            return low not in ("false", "0", "no", "ground", "grounded", "none", "")
        number = as_number(air)
        if number is not None:
            return number > 0
        # 没有 air 字段：用 y 相对首帧基准的偏移判断离地（兼容 y=高度或 y=屏幕坐标）。
        y = memory.get("y")
        if y is None:
            return False
        base = self.ground_y[seat]
        if base is None:
            self.ground_y[seat] = y
            return False
        if abs(y - base) > 24.0:
            self.air_run[seat] += 1
            if self.air_run[seat] > 150:
                # 连续“离地”太久更像 y 语义不同：重新取基准，避免永远误判。
                self.ground_y[seat] = y
                self.air_run[seat] = 0
                return False
            return True
        self.air_run[seat] = 0
        return False

    def _state_class(self, seat):
        return classify_state(self.known[seat].get("state"))

    def _near_known_wall(self, seat):
        x = self.known[seat].get("x")
        if x is None:
            return False
        if self.wall_left is not None and self.wall_left_seen >= 3 and abs(x - self.wall_left) <= 25:
            return True
        if self.wall_right is not None and self.wall_right_seen >= 3 and abs(x - self.wall_right) <= 25:
            return True
        return False

    def _bar_stage(self, seat, field, bars=1.0):
        """判断某条资源是否至少攒够 bars 格（自适应 0..1 / 0..3 / 0..10 等刻度）。

        资料显示“气条”最多 3 格：必杀（I）耗 1 格，三格必杀或变身耗 3 格。
        返回 None 表示还无法判断（字段缺失或还没观测到非零值）。
        """
        value = self.known[seat].get(field)
        if value is None:
            return None
        scale = self._resource_scale(self.bar_peak[seat].get(field), field)
        if scale is None:
            return None
        return value >= scale * bars / 3.0 - 1e-6

    def _skill_ok(self):
        # 技能（U）基本不耗气，只看按键冷却节奏。
        return self._pulse_ready("skill")

    def _super_ok(self):
        # 必杀（I）耗一格气：只认 qi（气，实测 0..300、一格 100）。
        # energy 是耐力（防御/瞬步/受击消耗），不能当必杀资源用。
        if not self._pulse_ready("super"):
            return False
        qi = self._bar_stage(self.seat, "qi", 1.0)
        if qi is not None:
            return qi
        # 没有 qi 字段时用时钟兜底（避免整局不用必杀）。
        return self.clock >= 900

    def _assist_ok(self):
        return self._pulse_ready("assist")

    def _stamina_low(self):
        """耐力（energy）低于两成时防御容易破防，改成幽步/后撤/跳跃拉开。"""
        ratio = self._bar_ratio(self.seat, "energy")
        return ratio is not None and ratio < 0.20

    # -------------------------------------------------------------- 按键输出

    def _pulse_ready(self, button):
        return self.clock >= self.cooldown.get(button, 0)

    def _compose(self, horizontal=None, vertical=None, pulse=None):
        buttons = []
        pressed = []
        if horizontal in HORIZONTAL:
            buttons.append(horizontal)
        if vertical in VERTICAL:
            buttons.append(vertical)
        if pulse in PULSE_GAP and self._pulse_ready(pulse) and pulse not in self.last_buttons:
            buttons.append(pulse)
            pressed.append(pulse)
        buttons = self._sanitize(buttons)
        for button in pressed:
            if button in buttons:
                self.cooldown[button] = self.clock + PULSE_GAP[button]
        return buttons

    @staticmethod
    def _sanitize(buttons):
        """保证按键集合合法：不重复、不同向、不混用绝对/相对水平方向。"""
        result = []
        used_horizontal = None
        used_vertical = None
        for button in buttons:
            if not isinstance(button, str) or button not in BUTTONS:
                continue
            if button in result:
                continue
            if button in HORIZONTAL:
                if used_horizontal is not None:
                    continue
                used_horizontal = button
            if button in VERTICAL:
                if used_vertical is not None:
                    continue
                used_vertical = button
            result.append(button)
        return result

    # -------------------------------------------------------------- 决策入口

    def _decide(self):
        my_hp = self._hp_ratio(self.seat)
        foe_hp = self._hp_ratio(3 - self.seat)
        gap = self._gap()
        my_air = self._airborne(self.seat)
        foe_air = self._airborne(3 - self.seat)
        me_state = self._state_class(self.seat)
        foe_state = self._state_class(3 - self.seat)
        since_dealt = self.clock - self.last_hit_dealt_clock
        since_taken = self.clock - self.last_hit_taken_clock
        skill_ok = self._skill_ok()
        super_ok = self._super_ok()
        assist_ok = self._assist_ok()

        # 0) 有一方已倒下：这一小回合结束，等裁判换人/结算，不做无效操作。
        if (my_hp is not None and my_hp <= 0.0) or (foe_hp is not None and foe_hp <= 0.0):
            return []

        # 0.5) 自己处于受击/倒地硬直：动作会被游戏忽略，提前按住防御等起身格挡。
        if me_state in ("stunned", "down"):
            return self._compose(None, "down")

        # 1) 自己在空中：向对手方向飘并压空中攻击。
        if my_air:
            return self._air_plan(gap)

        # 2) 刚挨打：对手在出招就先躲/防；对手收招后立刻反打。
        #    实测技能/必杀（state 11/12）常带破防或远程判定，举盾会被打穿，改用位移躲开。
        if since_taken <= 16:
            if foe_state == "attacking":
                raw_state = self.known[3 - self.seat].get("state")
                if raw_state in (11, 12):
                    return self._evade_special(gap)
                return self._escape(gap)
            if since_taken > 3:
                return self._counter(gap, skill_ok, super_ok)
            return self._escape(gap)

        # 3) 刚打中对手：连段确认/必杀收尾。
        if since_dealt <= 12 and (gap is None or gap < 240):
            return self._confirm(gap, foe_hp, skill_ok, super_ok, assist_ok)

        # 4) 对手起跳：防空。
        if foe_air and (gap is None or gap < 185):
            return self._antiair(gap)

        # 5) 对手正在出招：
        #    实测技能/必杀（state 11/12）会打穿防御并造成大伤害，优先用位移躲开；
        #    普通攻击（state 10）才用防御/幽步/抢招应对。
        if foe_state == "attacking":
            raw_state = self.known[3 - self.seat].get("state")
            if raw_state in (11, 12):
                if gap is not None and gap < 340:
                    return self._evade_special(gap)
            else:
                if gap is not None and gap < 165:
                    return self._respond_attack(gap, me_state, raw_state)

        # 6) 对手僵直：立刻惩罚；对手收招硬直同样可以抢招。
        if foe_state in ("stunned", "recovering"):
            return self._punish(gap, super_ok)

        # 6.5) 对手倒地：攻击会落空，先贴近准备压制或举盾等其起身。
        if foe_state == "down":
            if gap is not None and gap > 140:
                return self._compose("forward")
            return self._compose(None, "down")

        # 7) 近身且血量劣势：拉开距离（保留反打机会）。
        if (gap is not None and gap < 120 and self._behind(my_hp, foe_hp)
                and self.rng.random() < 0.35):
            return self._disengage()

        # 8) 未观测到横向位置：先向前压并试探。
        if gap is None:
            return self._compose("forward", None, "attack")

        # 9) 距离分层。
        if gap > 310:
            return self._rush(gap, skill_ok)
        if gap > 235:
            return self._approach(gap, skill_ok, assist_ok)
        if gap > 150:
            return self._mid(gap, skill_ok, assist_ok, super_ok, foe_hp)
        if gap > 85:
            return self._neutral(gap, skill_ok, assist_ok, super_ok, foe_hp, foe_state)
        return self._pressure(gap, skill_ok, assist_ok, super_ok, foe_hp, foe_state)

    # -------------------------------------------------------------- 各分支

    @staticmethod
    def _behind(my_hp, foe_hp):
        if my_hp is None or foe_hp is None:
            return False
        return my_hp + 0.12 < foe_hp

    def _air_plan(self, gap):
        if gap is None or gap < 160:
            return self._compose("forward", None, "attack")
        if gap < 280:
            return self._compose("forward", None, "attack")
        return self._compose("forward")

    def _escape(self, gap):
        roll = self.rng.random()
        # 受击瞬间按援助可以尝试灵替/灵爆（耗一格气），这是最有效的脱连手段。
        lingti_ok = self._bar_stage(self.seat, "qi", 1.0)
        if (self._assist_ok() and self.clock - self.last_hit_taken_clock <= 6
                and lingti_ok is not False and roll < 0.55):
            return self._compose(None, None, "assist")
        if self._stamina_low() and roll < 0.5:
            # 耐力见底时硬防会被破防，直接换位或抢招。
            return self._compose("back", None, "jump")
        # 贴身：防御（S/↓）与幽步（S+L）为主，混后撤。
        if gap is not None and gap < 70:
            if roll < 0.34:
                return self._compose(None, "down")
            if roll < 0.62:
                return self._compose(None, "down", "dash")
            if roll < 0.82:
                return self._compose("back", None, "dash")
            return self._compose("back", None, "jump")
        # 中距离：短防一下等对手收招，或后撤重新整理。
        if roll < 0.34:
            return self._compose(None, "down")
        if roll < 0.52:
            return self._compose(None, "down", "dash")
        if roll < 0.76:
            return self._compose("back", None, "dash")
        if roll < 0.88:
            return self._compose("back")
        return self._compose("back", None, "jump")

    def _counter(self, gap, skill_ok, super_ok):
        """对手收招后的反打：优先用必杀/技能惩罚硬直，否则普攻起手。"""
        if gap is None:
            return self._compose("forward", None, "attack")
        if super_ok and gap < 135 and self.rng.random() < 0.30:
            return self._compose("forward", None, "super")
        if skill_ok and self.rng.random() < 0.25:
            return self._compose("forward", None, "skill")
        if gap < 80 and self.rng.random() < 0.30:
            return self._compose("forward", "down", "attack")
        return self._compose("forward", None, "attack")

    def _confirm(self, gap, foe_hp, skill_ok, super_ok, assist_ok):
        if gap is not None and gap > 230:
            return self._compose("forward")
        if (super_ok and gap is not None and gap < 145
                and (foe_hp is None or foe_hp < 0.9) and self.rng.random() < 0.50):
            return self._compose("forward", None, "super")
        if skill_ok and self.rng.random() < 0.32:
            return self._compose("forward", None, "skill")
        if assist_ok and gap is not None and gap < 160 and self.rng.random() < 0.14:
            return self._compose("forward", None, "assist")
        if gap is not None and gap < 75 and self.rng.random() < 0.30:
            return self._compose("forward", "down", "attack")
        return self._compose("forward", None, "attack")

    def _antiair(self, gap):
        if gap is not None and gap <= 105:
            if self._pulse_ready("jump"):
                return self._compose(None, None, "jump")
            return self._compose("forward", None, "attack")
        if gap is not None and gap <= 195:
            if self._pulse_ready("jump"):
                return self._compose("forward", None, "jump")
            return self._compose("forward", None, "attack")
        return self._compose("forward", None, "attack")

    def _evade_special(self, gap):
        """对手技能/必杀：以位移躲避为主，别硬盾（实测会被破防打穿）。"""
        roll = self.rng.random()
        if self._stamina_low():
            if roll < 0.5:
                return self._compose("back", None, "dash")
            return self._compose("back", None, "jump")
        if gap is not None and gap < 90:
            # 贴身：幽步/后跃脱出，随后再惩罚对手收招
            if roll < 0.45:
                return self._compose(None, "down", "dash")
            if roll < 0.75:
                return self._compose("back", None, "jump")
            return self._compose("back", None, "dash")
        if roll < 0.55:
            return self._compose("back", None, "dash")
        if roll < 0.80:
            return self._compose("back", None, "jump")
        if roll < 0.92:
            return self._compose("back")
        # 少量用远程技能对换/打断
        return self._compose("back", None, "skill")

    def _respond_attack(self, gap, me_state, foe_raw=None):
        roll = self.rng.random()
        # 耐力见底时防御会被破防：优先幽步/后撤/跳走，不硬防。
        if self._stamina_low():
            if roll < 0.45:
                return self._compose(None, "down", "dash")
            if roll < 0.75:
                return self._compose("back", None, "dash")
            return self._compose("back", None, "jump")
        # 远距离的远程技能/必杀：举盾或后撤，不隔着距离抢招。
        if gap is not None and gap > 150:
            if roll < 0.62:
                return self._compose(None, "down")
            if roll < 0.84:
                return self._compose("back")
            return self._compose(None, "down", "dash")
        # 自己也在出招时偶发对拼，避免一直被打断。
        if me_state == "attacking" and roll < 0.35:
            return self._compose("forward", None, "attack")
        # 连续防御会掉耐力并可能被破防：防几帧就主动换位。
        if self.guard_streak >= 7:
            if roll < 0.42:
                return self._compose(None, "down", "dash")   # 幽步
            if roll < 0.74:
                return self._compose("back", None, "dash")
            return self._compose("back", None, "jump")
        if roll < 0.52:
            return self._compose(None, "down")
        if roll < 0.70:
            return self._compose(None, "down", "dash")
        if roll < 0.86:
            return self._compose("back")
        return self._compose("forward", None, "attack")

    def _punish(self, gap, super_ok):
        if gap is None:
            return self._compose("forward", None, "attack")
        if super_ok and gap < 115 and self.rng.random() < 0.20:
            return self._compose("forward", None, "super")
        if gap > 200:
            return self._compose("forward", None, "dash")
        if gap > 105:
            return self._compose("forward", None, "attack")
        if self.rng.random() < 0.30:
            return self._compose("forward", "down", "attack")
        return self._compose("forward", None, "attack")

    def _disengage(self):
        if self.rng.random() < 0.5:
            return self._compose("back", None, "dash")
        return self._compose(None, "down", "dash")

    def _rush(self, gap, skill_ok):
        if skill_ok and self.rng.random() < 0.18:
            return self._compose("forward", None, "skill")
        if self._pulse_ready("dash") and self.rng.random() < 0.35:
            return self._compose("forward", None, "dash")
        if self.rng.random() < 0.15 and self._pulse_ready("jump"):
            return self._compose("forward", None, "jump")
        return self._compose("forward")

    def _approach(self, gap, skill_ok, assist_ok):
        if skill_ok and self.rng.random() < 0.26:
            return self._compose("forward", None, "skill")
        if assist_ok and self.rng.random() < 0.16:
            return self._compose("forward", None, "assist")
        if self.closing > 1.0:
            # 对手也在逼近：站防让招、后退或抢招，避免硬吃冲刺。
            roll = self.rng.random()
            if roll < 0.18:
                return self._compose(None, "down")
            if roll < 0.42:
                return self._compose("back")
            return self._compose("forward", None, "attack")
        if self._pulse_ready("dash") and self.rng.random() < 0.22:
            return self._compose("forward", None, "dash")
        return self._compose("forward")

    def _mid(self, gap, skill_ok, assist_ok, super_ok, foe_hp):
        if skill_ok and self.rng.random() < 0.34:
            return self._compose("forward", None, "skill")
        if assist_ok and self.rng.random() < 0.14:
            return self._compose("forward", None, "assist")
        roll = self.rng.random()
        if roll < 0.14:
            return self._compose("forward", None, "jump")
        if roll < 0.26:
            return self._compose(None, "down")
        if roll < 0.36 and self.closing < 0.6:
            return self._compose("back")
        if roll < 0.76:
            return self._compose("forward", None, "attack")
        if roll < 0.88:
            return self._compose("forward", None, "dash")
        if super_ok and (foe_hp is None or foe_hp < 0.85):
            return self._compose("forward", None, "super")
        return self._compose("forward", None, "attack")

    def _neutral(self, gap, skill_ok, assist_ok, super_ok, foe_hp, foe_state):
        if skill_ok and self.rng.random() < 0.20:
            return self._compose("forward", None, "skill")
        if assist_ok and self.rng.random() < 0.12:
            return self._compose("forward", None, "assist")
        if self._near_known_wall(3 - self.seat) and self.rng.random() < 0.25:
            # 对手靠墙：更主动地推进压制。
            return self._compose("forward", None, "attack")
        roll = self.rng.random()
        if roll < 0.36:
            return self._compose("forward", None, "attack")
        if roll < 0.46:
            return self._compose(None, "down")
        if roll < 0.58:
            return self._compose("back")
        if roll < 0.72:
            return self._compose(None, "down", "attack")
        if roll < 0.82:
            return self._compose(None, "down", "dash")
        if roll < 0.90:
            return self._compose("forward", None, "jump")
        if roll < 0.96:
            return self._compose("forward", None, "dash")
        if super_ok and (foe_hp is None or foe_hp < 0.85):
            return self._compose("forward", None, "super")
        return self._compose("back", None, "dash")

    def _pressure(self, gap, skill_ok, assist_ok, super_ok, foe_hp, foe_state):
        if foe_state == "blocking":
            # 对手防住：换下段/技能/援助破防，而不是一直站桩普攻。
            roll = self.rng.random()
            if skill_ok and roll < 0.45:
                return self._compose("forward", None, "skill")
            if assist_ok and roll < 0.68:
                return self._compose("forward", None, "assist")
            return self._compose("forward", "down", "attack")
        if super_ok and self.rng.random() < 0.16:
            return self._compose("forward", None, "super")
        if self._near_known_wall(self.seat) and self.rng.random() < 0.18:
            # 自己靠墙：跳出去重新拉开。
            return self._compose("forward", None, "jump")
        roll = self.rng.random()
        if roll < 0.56:
            return self._compose("forward", None, "attack")
        if roll < 0.68:
            return self._compose("forward", "down", "attack")
        if roll < 0.73:
            return self._compose(None, "down")
        if roll < 0.78:
            return self._compose("back")
        if roll < 0.90 and skill_ok:
            return self._compose("forward", None, "skill")
        if roll < 0.96 and assist_ok:
            return self._compose("forward", None, "assist")
        return self._compose("forward", None, "attack")

    # -------------------------------------------------------------- 内部工具

    def _remember(self):
        self.previous = {1: dict(self.known[1]), 2: dict(self.known[2])}

    def _log(self, kind, payload):
        if self.log_handle is None:
            return
        try:
            record = {"kind": kind, "clock": self.clock, "matchId": self.match_id}
            record.update(payload)
            self.log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.log_handle.flush()
        except (OSError, TypeError, ValueError):
            pass


# ---------------------------------------------------------------- 主循环

def iter_input_lines(stream):
    """按字节读 UTF-8，规避 Windows 控制台默认编码；无 buffer 时退回文本读。"""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        for raw in buffer:
            yield raw.decode("utf-8", "replace")
    else:
        for raw in stream:
            yield raw


def write_output(stream, text):
    """以 UTF-8 + '\\n' 写出并立即刷新（不经文本层，杜绝 CRLF 与编码问题）。"""
    data = (text + "\n").encode("utf-8")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        stream.write(text + "\n")
        stream.flush()


def main():
    ai = CombatAI()
    try:
        for line in iter_input_lines(sys.stdin):
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            try:
                if kind == "draft":
                    reply = ai.on_draft(msg)
                elif kind == "frame":
                    reply = ai.on_frame(msg)
                else:
                    continue
            except Exception:
                reply = ai.safe_reply(msg)
                ai._reset_short_term("decision_error")
                if reply is None:
                    continue
            try:
                text = json.dumps(reply, ensure_ascii=True, separators=(",", ":"),
                                  allow_nan=False)
            except (TypeError, ValueError):
                continue
            if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
                continue
            try:
                write_output(sys.stdout, text)
            except (OSError, ValueError):
                return
    except (KeyboardInterrupt, BrokenPipeError):
        return


if __name__ == "__main__":
    main()
