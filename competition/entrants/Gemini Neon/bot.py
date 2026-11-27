"""Gemini 4 Neon - State-driven adaptive footsies, hit-confirm, and counter bot.

Strictly complies with BvN 3.8.8.5 Local AI Competition Protocol 1.
Uses only Python standard library and public referee observations via stdin/stdout.
"""

from collections import deque
import json
import math
import sys


VALID_BUTTONS = frozenset((
    "left", "right", "forward", "back", "up", "down",
    "attack", "jump", "dash", "skill", "super", "assist",
))
PULSE_BUTTONS = frozenset(("attack", "jump", "dash", "skill", "super", "assist"))

# BvN 3.8.8.5 observed integer state semantics
ST_FREE = 0
ST_ATK = 10
ST_SKILL = 11
ST_SUPER = 12
ST_ULTRA = 13
ST_JUMP = 14
ST_DASH = 15
ST_MOVE = 16
ST_GUARD = 20
ST_HURT = 21
ST_FLY = 22
ST_DOWN = 23
ST_GETUP = 24
ST_SPECIAL = 40

# Role-based fighter tier preferences refined by real match telemetry
POINT_PREFS = (
    "aizen", "tensa", "ichigo_zero", "byakuya", "ogihci",
    "ulquiorra", "naruto_kyubi", "madara", "pain", "itachi",
    "kakashi_kamui", "sasuke", "grimmjow", "yoruichi", "lee",
)
MID_PREFS = (
    "byakuya", "aizen", "ichigo_zero", "tensa", "madara",
    "pain", "aizen_captain", "naruto_kyubi", "ogihci", "starrk",
    "toushirou", "kakashi_gsw", "obito", "gaara", "onoki",
)
ANCHOR_PREFS = (
    "tensa", "ichigo_zero", "aizen", "ogihci", "madara",
    "byakuya", "kenpachi", "naruto_kyubi", "aizen_captain", "pain",
    "killerbee", "kakashi_kamui", "ichigo", "naruto", "sasuke",
)
ASSIST_PREFS = (
    "aizen_muken", "ichigo_dangai", "ogihci_vasto_lord", "yamamoto",
    "sasuke_amenotejikara", "minato", "itachi", "byakuya", "madara",
    "gaara", "starrk_resurrection", "harribel_resurrection", "obito",
    "toushirou", "uryuu", "shunsui", "urahara", "orihime",
)

# String fallback keywords if any fighter reports string states
STR_ATTACK = ("attack", "atk", "strike", "slash", "combo")
STR_SKILL = ("skill", "special", "cast", "shoot")
STR_SUPER = ("super", "ulti", "bankai", "final", "burst", "oogi")
STR_HURT = ("hurt", "hit", "stun", "damage")
STR_FLY = ("fly", "blown", "knock", "launch")
STR_DOWN = ("down", "lie", "ground", "ko", "dead")
STR_GUARD = ("guard", "block", "defend", "parry")
STR_DASH = ("dash", "shunpo", "step", "rush", "teleport")
STR_AIR = ("jump", "air", "hop", "fall", "float")


def safe_float(value, default=None):
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
        return num if math.isfinite(num) else default
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_token(text):
    if text is None:
        return ""
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def sanitize_buttons(buttons):
    """Deduplicate and enforce protocol directional mutual-exclusion rules."""
    if not buttons:
        return []
    result = []
    seen = set()
    has_abs_h = False
    has_rel_h = False
    has_v = False
    for btn in buttons:
        if not isinstance(btn, str) or btn not in VALID_BUTTONS or btn in seen:
            continue
        if btn in ("left", "right"):
            if has_abs_h or has_rel_h:
                continue
            has_abs_h = True
        elif btn in ("forward", "back"):
            if has_rel_h or has_abs_h:
                continue
            has_rel_h = True
        elif btn in ("up", "down"):
            if has_v:
                continue
            has_v = True
        seen.add(btn)
        result.append(btn)
    return result


def score_candidate(cand_id, pref_tuple, index_in_pool):
    low = str(cand_id).lower()
    norm = normalize_token(cand_id)
    best = 0.0
    for rank, key in enumerate(pref_tuple):
        weight = float(len(pref_tuple) - rank)
        key_norm = normalize_token(key)
        if low == key or norm == key_norm:
            best = max(best, 120.0 + weight * 3.0)
        elif key_norm and key_norm in norm:
            best = max(best, 65.0 + weight * 1.5)
    return (best, -index_in_pool)


def draft_team(msg):
    """Blind draft strictly from runtime fighters & assists lists."""
    raw_f = msg.get("fighters") or []
    fighters = list(dict.fromkeys(f for f in raw_f if isinstance(f, str) and f))
    raw_a = msg.get("assists") or []
    assists = list(dict.fromkeys(a for a in raw_a if isinstance(a, str) and a))

    team_size = min(max(1, safe_int(msg.get("teamSize"), 1)), len(fighters))
    seat = safe_int(msg.get("seat"), 1)
    score = msg.get("score")
    my_s, opp_s = 0, 0
    if isinstance(score, (list, tuple)) and len(score) >= 2:
        idx = 0 if seat == 1 else 1
        my_s = safe_int(score[idx], 0)
        opp_s = safe_int(score[1 - idx], 0)

    if team_size == 1:
        role_plan = [POINT_PREFS]
    elif team_size == 2:
        role_plan = [POINT_PREFS, MID_PREFS]
    elif (my_s + opp_s) % 2 == 1:
        role_plan = [POINT_PREFS, MID_PREFS, ANCHOR_PREFS]
    else:
        role_plan = [POINT_PREFS, MID_PREFS, ANCHOR_PREFS]

    pool_idx = {fid: i for i, fid in enumerate(fighters)}
    avail = list(fighters)
    roster = []
    for slot in range(team_size):
        prefs = role_plan[slot % len(role_plan)]
        chosen = max(avail, key=lambda fid: score_candidate(fid, prefs, pool_idx[fid]))
        roster.append(chosen)
        avail.remove(chosen)

    assist = ""
    if assists:
        a_idx = {aid: i for i, aid in enumerate(assists)}
        scored = [(score_candidate(aid, ASSIST_PREFS, a_idx[aid]), aid) for aid in assists]
        best_sc, best_a = max(scored, key=lambda x: x[0])
        assist = best_a if best_sc[0] > 0.0 else assists[(my_s + opp_s) % len(assists)]

    return {"type": "pick", "roster": roster, "assist": assist}


def decode_state(raw_state):
    """Decode BvN 3.8.8.5 integer states with string/missing fallback."""
    tags = {
        "free": False, "attack": False, "skill": False, "super": False,
        "offense": False, "guard": False, "hurt": False, "fly": False,
        "down": False, "getup": False, "dash": False, "air": False,
        "special": False, "known": False,
    }
    if raw_state is None:
        return None, tags

    num = None
    if isinstance(raw_state, (int, float)) and not isinstance(raw_state, bool):
        num = int(raw_state)
    elif isinstance(raw_state, str) and raw_state.strip().lstrip("-").isdigit():
        num = int(raw_state.strip())

    if num is not None:
        tags["known"] = True
        if num in (ST_FREE, ST_MOVE):
            tags["free"] = True
        elif num == ST_ATK:
            tags["attack"] = True
            tags["offense"] = True
        elif num == ST_SKILL:
            tags["skill"] = True
            tags["offense"] = True
        elif num in (ST_SUPER, ST_ULTRA):
            tags["super"] = True
            tags["offense"] = True
        elif num == ST_JUMP:
            tags["air"] = True
        elif num == ST_DASH:
            tags["dash"] = True
        elif num == ST_GUARD:
            tags["guard"] = True
        elif num == ST_HURT:
            tags["hurt"] = True
        elif num == ST_FLY:
            tags["hurt"] = True
            tags["fly"] = True
            tags["air"] = True
        elif num == ST_DOWN:
            tags["down"] = True
        elif num == ST_GETUP:
            tags["getup"] = True
        elif num == ST_SPECIAL:
            tags["special"] = True
            tags["offense"] = True
        return num, tags

    text = str(raw_state).lower()
    tags["known"] = bool(text)
    tags["attack"] = any(k in text for k in STR_ATTACK)
    tags["skill"] = any(k in text for k in STR_SKILL)
    tags["super"] = any(k in text for k in STR_SUPER)
    tags["offense"] = tags["attack"] or tags["skill"] or tags["super"]
    tags["hurt"] = any(k in text for k in STR_HURT)
    tags["fly"] = any(k in text for k in STR_FLY)
    tags["down"] = any(k in text for k in STR_DOWN)
    tags["guard"] = any(k in text for k in STR_GUARD)
    tags["dash"] = any(k in text for k in STR_DASH)
    tags["air"] = any(k in text for k in STR_AIR)
    tags["free"] = not (tags["offense"] or tags["hurt"] or tags["fly"] or tags["down"] or tags["guard"] or tags["dash"])
    return None, tags


def parse_fighter(raw):
    if not isinstance(raw, dict):
        raw = {}
    fid = raw.get("id") or raw.get("name") or ""
    x = safe_float(raw.get("x"), None)
    y = safe_float(raw.get("y"), None)
    hp = safe_float(raw.get("hp"), None)
    hp_max = safe_float(raw.get("hpMax"), None)
    if hp_max is not None and hp_max <= 0:
        hp_max = None
    energy = safe_float(raw.get("energy"), None)
    qi = safe_float(raw.get("qi"), None)
    facing = safe_int(raw.get("facing"), 0)
    if facing not in (1, -1):
        facing = 0
    state_num, tags = decode_state(raw.get("state"))
    air_raw = raw.get("air")
    if isinstance(air_raw, bool):
        in_air = air_raw
    elif air_raw is not None:
        in_air = bool(safe_float(air_raw, 0.0))
    else:
        in_air = tags["air"]
    return {
        "id": str(fid), "x": x, "y": y, "hp": hp, "hp_max": hp_max,
        "energy": energy, "qi": qi, "facing": facing,
        "air": in_air, "air_explicit": air_raw is not None,
        "state": state_num, "tags": tags,
    }


def fighter_profile(fid):
    norm = normalize_token(fid)
    if any(k in norm for k in ("gaara", "byakuya", "temari", "uryuu", "deidara", "starrk", "onoki", "konan", "ulquiorra")):
        return {"style": "zoner", "atk_r": 74.0, "skill_r": 185.0, "dash_r": 265.0}
    if any(k in norm for k in ("lee", "kenpachi", "grimmjow", "yoruichi", "neji", "soifon", "tensa", "ichigozero", "ogihci", "killerbee")):
        return {"style": "rush", "atk_r": 66.0, "skill_r": 135.0, "dash_r": 210.0}
    return {"style": "balanced", "atk_r": 72.0, "skill_r": 160.0, "dash_r": 235.0}



class CombatBot:
    """Real-time state-driven combat AI with facing guard & cancel-chain engine."""

    def __init__(self):
        self.prev_tick = None
        self.prev_round = None
        self.frame_count = 0
        self.last_buttons = []

        self.my_id = ""
        self.enemy_id = ""
        self.profile = {"style": "balanced", "atk_r": 72.0, "skill_r": 160.0, "dash_r": 235.0}
        self.my_hp_max = None
        self.enemy_hp_max = None
        self.prev_my_hp = None
        self.prev_enemy_hp = None
        self.prev_enemy_st = None
        self.prev_my_st = None
        self.prev_dist = None

        self.stage_min_x = None
        self.stage_max_x = None
        self.ground_y = None

        self.combo_hits = 0
        self.atk_pulses = 0
        self.hit_confirm_until = -1
        self.punish_until = -1
        self.guard_ticks = 0
        self.hurt_ticks = 0
        self.stall_ticks = 0
        self.mix_counter = 0

        self.last_assist_tick = -1000
        self.last_super_tick = -1000
        self.last_dash_tick = -1000
        self.last_jump_tick = -1000
        self.last_skill_tick = -1000
        self.est_qi = 100.0

        self.opp_offense_hist = deque(maxlen=40)
        self.opp_guard_hist = deque(maxlen=40)

    def _reset_round(self, me, enemy, rd):
        self.prev_round = rd
        self.last_buttons = []
        if me["id"]:
            self.my_id = me["id"]
            self.profile = fighter_profile(self.my_id)
        if enemy["id"]:
            self.enemy_id = enemy["id"]
        self.my_hp_max = me["hp_max"] or me["hp"]
        self.enemy_hp_max = enemy["hp_max"] or enemy["hp"]
        self.prev_my_hp = me["hp"]
        self.prev_enemy_hp = enemy["hp"]
        self.prev_my_st = me["state"]
        self.prev_enemy_st = enemy["state"]
        self.prev_dist = None
        self.combo_hits = 0
        self.atk_pulses = 0
        self.hit_confirm_until = -1
        self.punish_until = -1
        self.guard_ticks = 0
        self.hurt_ticks = 0
        self.stall_ticks = 0
        self.est_qi = me["qi"] if me["qi"] is not None else 100.0

    def _emit(self, raw_buttons, release_fallback=None):
        """Sanitize buttons and guarantee rising-edge release on pulse keys."""
        btns = sanitize_buttons(raw_buttons)
        if btns and self.last_buttons:
            prev_set = set(self.last_buttons)
            if any(b in PULSE_BUTTONS and b in prev_set for b in btns):
                if release_fallback is not None:
                    btns = sanitize_buttons(release_fallback)
                else:
                    btns = [b for b in btns if b not in PULSE_BUTTONS]
        self.last_buttons = list(btns)
        return btns

    def _qi(self, me):
        return me["qi"] if me["qi"] is not None else self.est_qi

    def _energy(self, me):
        return me["energy"] if me["energy"] is not None else 80.0

    def _super_ready(self, me, tick):
        return self._qi(me) >= 99.5 and (tick - self.last_super_tick) >= 26

    def _assist_ready(self, tick):
        return (tick - self.last_assist_tick) >= 220


    def _update_geo(self, dt, me, enemy):
        dist, dy, closing, wrong_facing = None, 0.0, 0.0, False
        am_cornered, enemy_cornered = False, False
        if me["x"] is not None and enemy["x"] is not None:
            dx_s = enemy["x"] - me["x"]
            dist = abs(dx_s)
            lo_x, hi_x = min(me["x"], enemy["x"]), max(me["x"], enemy["x"])
            self.stage_min_x = lo_x if self.stage_min_x is None else min(self.stage_min_x, lo_x)
            self.stage_max_x = hi_x if self.stage_max_x is None else max(self.stage_max_x, hi_x)
            if self.prev_dist is not None and dt > 0:
                closing = (self.prev_dist - dist) / float(max(1, min(dt, 8)))
            if me["facing"] in (1, -1) and dist > 4.0:
                wrong_facing = (dx_s * me["facing"]) < -4.0
            if (self.stage_max_x - self.stage_min_x) >= 320.0:
                am_cornered = (me["x"] - self.stage_min_x < 75.0 and dx_s > 0) or (self.stage_max_x - me["x"] < 75.0 and dx_s < 0)
                enemy_cornered = (enemy["x"] - self.stage_min_x < 75.0 and dx_s < 0) or (self.stage_max_x - enemy["x"] < 75.0 and dx_s > 0)

        if me["y"] is not None and not me["air"]:
            if self.ground_y is None:
                self.ground_y = me["y"]
            elif abs(me["y"] - self.ground_y) < 15.0:
                self.ground_y = self.ground_y * 0.8 + me["y"] * 0.2
        if me["y"] is not None and enemy["y"] is not None:
            dy = enemy["y"] - me["y"]

        enemy_in_air = enemy["air"] or (
            not enemy["air_explicit"]
            and enemy["y"] is not None
            and self.ground_y is not None
            and abs(enemy["y"] - self.ground_y) > 22.0
        )
        return dist, dy, closing, wrong_facing, am_cornered, enemy_cornered, enemy_in_air


    def _observe(self, msg):
        tick = safe_int(msg.get("tick"), self.frame_count * 2 + 2)
        rd = msg.get("round")
        seat = safe_int(msg.get("seat"), 1)
        players = msg.get("players") if isinstance(msg.get("players"), list) else []
        own_idx = 0 if seat == 1 else 1
        me = parse_fighter(players[own_idx] if own_idx < len(players) else {})
        enemy = parse_fighter(players[1 - own_idx] if (1 - own_idx) < len(players) else {})

        self.frame_count += 1
        dt = 2 if self.prev_tick is None else (tick - self.prev_tick)

        hp_j_me = self.prev_my_hp is not None and me["hp"] is not None and self.my_hp_max and (me["hp"] - self.prev_my_hp) > self.my_hp_max * 0.25
        hp_j_en = self.prev_enemy_hp is not None and enemy["hp"] is not None and self.enemy_hp_max and (enemy["hp"] - self.prev_enemy_hp) > self.enemy_hp_max * 0.25
        swapped = (
            self.prev_tick is None or dt < 0 or dt > 180 or hp_j_me or hp_j_en
            or (rd is not None and self.prev_round is not None and rd != self.prev_round)
            or (self.my_id and me["id"] and me["id"] != self.my_id)
            or (self.enemy_id and enemy["id"] and enemy["id"] != self.enemy_id)
        )
        if swapped:
            self._reset_round(me, enemy, rd)
            dt = 2
        else:
            if rd is not None:
                self.prev_round = rd
            if me["id"] and not self.my_id:
                self.my_id, self.profile = me["id"], fighter_profile(me["id"])
            if enemy["id"] and not self.enemy_id:
                self.enemy_id = enemy["id"]
            if me["hp_max"]:
                self.my_hp_max = me["hp_max"]
            elif me["hp"] is not None and (self.my_hp_max is None or me["hp"] > self.my_hp_max):
                self.my_hp_max = me["hp"]
            if enemy["hp_max"]:
                self.enemy_hp_max = enemy["hp_max"]
            elif enemy["hp"] is not None and (self.enemy_hp_max is None or enemy["hp"] > self.enemy_hp_max):
                self.enemy_hp_max = enemy["hp"]

        took_dmg = self.prev_my_hp is not None and me["hp"] is not None and me["hp"] < self.prev_my_hp - 0.5
        dealt_dmg = self.prev_enemy_hp is not None and enemy["hp"] is not None and enemy["hp"] < self.prev_enemy_hp - 0.5

        if dealt_dmg:
            self.hit_confirm_until = tick + 18
            self.combo_hits += 1
            self.stall_ticks = 0
        elif not enemy["tags"]["hurt"] and tick > self.hit_confirm_until:
            self.combo_hits = 0
            self.atk_pulses = 0

        if me["tags"]["hurt"] or took_dmg:
            self.hurt_ticks += max(1, min(dt, 6))
            self.combo_hits = 0
            self.atk_pulses = 0
            self.stall_ticks = 0
        else:
            self.hurt_ticks = 0

        if me["tags"]["guard"]:
            self.guard_ticks += max(1, min(dt, 6))
        else:
            if self.guard_ticks > 0:
                self.punish_until = tick + 18
            self.guard_ticks = 0

        if self.prev_enemy_st in (ST_ATK, ST_SKILL, ST_SUPER, ST_ULTRA) and enemy["state"] == ST_FREE:
            self.punish_until = max(self.punish_until, tick + 14)
        if not dealt_dmg and not took_dmg:
            self.stall_ticks += max(1, min(dt, 6))

        self.est_qi = min(300.0, self.est_qi + max(1, min(dt, 6)) * 0.25 + (14.0 if dealt_dmg else 0.0) + (10.0 if took_dmg else 0.0))

        dist, dy, closing, wrong_facing, am_c, en_c, en_air = self._update_geo(dt, me, enemy)
        self.opp_offense_hist.append(1.0 if (enemy["tags"]["offense"] or closing > 3.0) else 0.0)
        self.opp_guard_hist.append(1.0 if enemy["tags"]["guard"] else 0.0)

        my_hp_r = (me["hp"] / self.my_hp_max) if (me["hp"] is not None and self.my_hp_max) else 1.0
        en_hp_r = (enemy["hp"] / self.enemy_hp_max) if (enemy["hp"] is not None and self.enemy_hp_max) else 1.0

        self.prev_tick, self.prev_my_hp, self.prev_enemy_hp = tick, me["hp"], enemy["hp"]
        self.prev_my_st, self.prev_enemy_st, self.prev_dist = me["state"], enemy["state"], dist

        return {
            "tick": tick, "dt": dt, "me": me, "enemy": enemy, "dist": dist, "dy": dy,
            "closing": closing, "wrong_facing": wrong_facing,
            "am_cornered": am_c, "enemy_cornered": en_c, "enemy_in_air": en_air,
            "took_dmg": took_dmg, "dealt_dmg": dealt_dmg,
            "my_hp_r": my_hp_r, "en_hp_r": en_hp_r, "hp_diff": my_hp_r - en_hp_r,
            "opp_offense": sum(self.opp_offense_hist) / float(len(self.opp_offense_hist)),
            "opp_guard": sum(self.opp_guard_hist) / float(len(self.opp_guard_hist)),
        }


    def _decide_hurt_or_busy(self, obs):
        tick, me, enemy, dist = obs["tick"], obs["me"], obs["enemy"], obs["dist"]
        energy = self._energy(me)

        # Launched/airborne hurt (state 22): spend 30 energy with jump for air recovery!
        if me["tags"]["fly"]:
            if self._assist_ready(tick) and dist is not None and dist < 140.0 and self.hurt_ticks >= 4:
                self.last_assist_tick = tick
                return self._emit(["assist"], release_fallback=[])
            if energy >= 35.0:
                return self._emit(["jump"], release_fallback=[])
            return self._emit(["down"])

        # Grounded hitstun (state 21): assist peel if available, else hold guard for any combo gap!
        if me["tags"]["hurt"]:
            if self._assist_ready(tick) and dist is not None and dist < 125.0 and (self.hurt_ticks >= 6 or obs["my_hp_r"] < 0.55):
                self.last_assist_tick = tick
                return self._emit(["assist"], release_fallback=["down"])
            return self._emit(["down"])

        if me["tags"]["down"] or me["tags"]["getup"]:
            if dist is not None and dist < 110.0 and enemy["tags"]["offense"]:
                return self._emit(["down"])
            return self._emit([])

        if me["tags"]["super"]:
            return self._emit([])

        # During skill (11) or special recovery (40): allow super cancel on hit, else avoid wasting cooldowns
        if me["tags"]["skill"] or me["tags"]["special"]:
            if (
                dist is not None
                and dist <= 155.0
                and abs(obs["dy"]) <= 95.0
                and not obs["wrong_facing"]
                and (enemy["tags"]["hurt"] or obs["dealt_dmg"])
                and self._super_ready(me, tick)
            ):
                self.last_super_tick = tick
                self.est_qi = max(0.0, self.est_qi - 100.0)
                cmd = ["up", "super"] if obs["dy"] < -30.0 else ["super"]
                return self._emit(cmd, release_fallback=[])
            if obs["wrong_facing"]:
                return self._emit(["forward"])
            return self._emit([])

        if me["tags"]["dash"]:
            if obs["wrong_facing"]:
                return self._emit(["forward"])
            if dist is not None and dist < 95.0 and enemy["tags"]["offense"]:
                return self._emit(["down"])
            return self._emit([])

        if me["air"] and not me["tags"]["free"]:
            if not obs["wrong_facing"] and dist is not None and dist < 105.0 and abs(obs["dy"]) < 95.0:
                if (tick - self.last_skill_tick) >= 24 and dist > 65.0:
                    self.last_skill_tick = tick
                    return self._emit(["skill"], release_fallback=["forward"])
                return self._emit(["attack"], release_fallback=["forward"])
            return self._emit(["forward"])

        return None

    def _decide_combo(self, obs):
        tick, me, enemy, dist, dy = obs["tick"], obs["me"], obs["enemy"], obs["dist"], obs["dy"]
        qi = self._qi(me)
        self.mix_counter += 1

        # Tighter, high-accuracy super window (<=105px, never whiff into invuln/dash/skill)
        if self._super_ready(me, tick) and dist <= 108.0 and abs(dy) <= 85.0:
            opp_vuln = enemy["tags"]["hurt"] or obs["dealt_dmg"] or (not enemy["tags"]["offense"] and not enemy["tags"]["dash"])
            should_super = opp_vuln and (
                obs["en_hp_r"] <= 0.50
                or obs["my_hp_r"] <= 0.42
                or me["tags"]["skill"]
                or self.combo_hits >= 1
                or self.atk_pulses >= 2
                or qi >= 165.0
                or obs["enemy_cornered"]
            )
            if should_super:
                if "super" not in self.last_buttons:
                    self.last_super_tick = tick
                    self.est_qi = max(0.0, self.est_qi - 100.0)
                if dy < -30.0:
                    cmd = ["up", "super"]
                elif dist <= 102.0 and qi >= 290.0:
                    cmd = ["down", "super"]
                else:
                    cmd = ["super"]
                return self._emit(cmd, release_fallback=[])

        if self._assist_ready(tick) and dist <= 145.0 and self.combo_hits >= 1:
            self.last_assist_tick = tick
            return self._emit(["assist"], release_fallback=[])

        if dist <= self.profile["atk_r"] + 22.0 and abs(dy) <= 70.0 and self.atk_pulses < 4:
            if "attack" not in self.last_buttons:
                self.atk_pulses += 1
            return self._emit(["attack"], release_fallback=[])

        if dist <= self.profile["skill_r"] and (tick - self.last_skill_tick >= 6 or me["tags"]["attack"]):
            self.last_skill_tick = tick
            if dy < -25.0:
                return self._emit(["up", "skill"], release_fallback=[])
            if dist <= 105.0 and self.mix_counter % 2 == 0:
                return self._emit(["down", "skill"], release_fallback=[])
            return self._emit(["skill"], release_fallback=[])

        return self._emit(["forward"])


    def _decide_neutral(self, obs):
        tick, me, enemy, dist = obs["tick"], obs["me"], obs["enemy"], obs["dist"]
        energy, qi = self._energy(me), self._qi(me)
        atk_r, skill_r, dash_r = self.profile["atk_r"] + 16.0, self.profile["skill_r"], self.profile["dash_r"]
        self.mix_counter += 1

        if enemy["tags"]["guard"] and dist <= 100.0:
            if energy >= 48.0 and not obs["enemy_cornered"] and (tick - self.last_dash_tick) >= 20:
                self.last_dash_tick = tick
                return self._emit(["forward", "dash"], release_fallback=["forward"])
            if self.mix_counter % 2 == 0:
                return self._emit(["down", "attack"], release_fallback=[])
            self.last_skill_tick = tick
            return self._emit(["down", "skill"], release_fallback=[])

        if dist <= atk_r:
            # High-meter dump or low-HP desperation super (strictly within accurate range & vulnerable opp)
            if (
                self._super_ready(me, tick)
                and dist <= 96.0
                and not enemy["tags"]["guard"]
                and not enemy["tags"]["dash"]
                and not enemy["tags"]["offense"]
                and (qi >= 180.0 or obs["my_hp_r"] <= 0.36 or obs["en_hp_r"] <= 0.34)
            ):
                if "super" not in self.last_buttons:
                    self.last_super_tick = tick
                    self.est_qi = max(0.0, self.est_qi - 100.0)
                cmd = ["down", "super"] if (qi >= 290.0 and dist <= 96.0) else ["super"]
                return self._emit(cmd, release_fallback=[])
            mod = self.mix_counter % 7
            if mod in (0, 1, 2, 4, 5):
                return self._emit(["attack"], release_fallback=[])
            if mod == 3:
                return self._emit(["down", "attack"], release_fallback=[])
            if (tick - self.last_skill_tick) >= 12:
                self.last_skill_tick = tick
                return self._emit(["down", "skill"], release_fallback=[])
            return self._emit(["attack"], release_fallback=[])

        if dist <= skill_r:
            if (
                self._super_ready(me, tick)
                and dist <= 105.0
                and not enemy["tags"]["guard"]
                and not enemy["tags"]["dash"]
                and not enemy["tags"]["offense"]
                and (obs["my_hp_r"] <= 0.34 or (qi >= 240.0 and obs["closing"] > 1.5))
            ):
                if "super" not in self.last_buttons:
                    self.last_super_tick = tick
                    self.est_qi = max(0.0, self.est_qi - 100.0)
                return self._emit(["super"], release_fallback=[])
            if self._assist_ready(tick) and (obs["enemy_cornered"] or obs["opp_offense"] > 0.25 or self.stall_ticks > 22):
                self.last_assist_tick = tick
                return self._emit(["assist"], release_fallback=["forward"])
            if (tick - self.last_skill_tick) >= 12 and (
                self.mix_counter % 3 == 0 or (self.profile["style"] == "zoner" and self.mix_counter % 2 == 0)
            ):
                self.last_skill_tick = tick
                return self._emit(["down", "skill"] if dist < 115.0 else ["skill"], release_fallback=["forward"])
            if (tick - self.last_jump_tick) >= 34 and dist > 95.0 and self.mix_counter % 7 == 0:
                self.last_jump_tick = tick
                return self._emit(["forward", "jump"], release_fallback=["forward"])
            return self._emit(["forward"])

        if dist <= dash_r:
            if (tick - self.last_skill_tick) >= 20 and self.profile["style"] == "zoner":
                self.last_skill_tick = tick
                return self._emit(["skill"], release_fallback=["forward"])
            return self._emit(["forward"])

        # Never dash directly into an opponent who is already attacking/casting
        if energy >= 48.0 and not enemy["tags"]["offense"] and (tick - self.last_dash_tick) >= 18:
            self.last_dash_tick = tick
            return self._emit(["forward", "dash"], release_fallback=["forward"])
        return self._emit(["forward"])


    def decide(self, msg):
        obs = self._observe(msg)
        tick, me, enemy, dist = obs["tick"], obs["me"], obs["enemy"], obs["dist"]

        busy = self._decide_hurt_or_busy(obs)
        if busy is not None:
            return busy

        if dist is None:
            self.mix_counter += 1
            if obs["took_dmg"]:
                return self._emit(["down"])
            if obs["dealt_dmg"] and self._super_ready(me, tick):
                self.last_super_tick = tick
                return self._emit(["super"], release_fallback=[])
            return self._emit(["attack"] if self.mix_counter % 2 == 0 else ["forward"], release_fallback=[])

        # Iron-clad facing guard (both in FREE and during ATTACK chains so we never whiff backward)
        if obs["wrong_facing"] and (me["tags"]["free"] or me["tags"]["attack"] or not me["tags"]["known"]):
            return self._emit(["forward"])

        energy = self._energy(me)
        # Escape enemy super / ultra (state 12 or 13) before getting hit!
        if enemy["tags"]["super"] and dist < 260.0:
            if energy >= 30.0 and not obs["am_cornered"] and dist > 62.0 and (tick - self.last_dash_tick) >= 14:
                self.last_dash_tick = tick
                return self._emit(["back", "dash"], release_fallback=["down"])
            if dist > 80.0 and (tick - self.last_jump_tick) >= 24:
                self.last_jump_tick = tick
                return self._emit(["back", "jump"], release_fallback=["down"])
            return self._emit(["down"])

        # Solid guard against enemy attacks/skills (never dash into mid-range projectiles!)
        threat_r = 148.0 if (enemy["tags"]["skill"] or enemy["tags"]["special"]) else 102.0
        if (enemy["tags"]["offense"] and dist < threat_r) or (me["tags"]["guard"] and enemy["tags"]["offense"] and dist < 168.0):
            if energy >= 24.0:
                return self._emit(["down"])
            if energy >= 20.0 and not obs["am_cornered"] and (tick - self.last_dash_tick) >= 14:
                self.last_dash_tick = tick
                return self._emit(["back", "dash"], release_fallback=["back"])
            if obs["am_cornered"] and energy >= 20.0 and (tick - self.last_dash_tick) >= 14:
                self.last_dash_tick = tick
                return self._emit(["forward", "dash"], release_fallback=["forward"])
            return self._emit(["back", "jump"], release_fallback=["back"])

        in_combo = (
            not enemy["tags"]["down"]
            and not enemy["tags"]["getup"]
            and dist <= 165.0
            and (
                enemy["tags"]["hurt"]
                or obs["dealt_dmg"]
                or tick <= self.hit_confirm_until
                or (me["tags"]["attack"] and dist <= self.profile["atk_r"] + 15.0)
            )
        )
        if in_combo:
            return self._decide_combo(obs)

        if tick <= self.punish_until and not enemy["tags"]["down"] and not enemy["tags"]["guard"] and dist <= self.profile["skill_r"]:
            self.punish_until = -1
            if dist <= 115.0 and self._super_ready(me, tick) and (obs["en_hp_r"] <= 0.42 or self._qi(me) >= 200.0):
                self.last_super_tick = tick
                self.est_qi = max(0.0, self.est_qi - 100.0)
                return self._emit(["super"], release_fallback=[])
            if dist <= self.profile["atk_r"] + 8.0:
                return self._emit(["attack"], release_fallback=[])
            self.last_skill_tick = tick
            return self._emit(["down", "skill"] if dist <= 100.0 else ["skill"], release_fallback=[])

        if enemy["tags"]["down"]:
            if dist < 62.0:
                return self._emit(["back"])
            if dist > 88.0:
                return self._emit(["forward"])
            return self._emit([])

        # Meaty okizeme pressure as opponent gets up (state 24)
        if enemy["tags"]["getup"]:
            if dist > 86.0:
                return self._emit(["forward"])
            if self._assist_ready(tick) and dist <= 120.0:
                self.last_assist_tick = tick
                return self._emit(["assist"], release_fallback=["forward"])
            if dist <= 76.0:
                return self._emit(["attack"], release_fallback=[])
            return self._emit(["forward"])

        if obs["enemy_in_air"] and not enemy["tags"]["hurt"] and dist <= 112.0:
            if dist <= 78.0:
                if (tick - self.last_skill_tick) >= 16:
                    self.last_skill_tick = tick
                    return self._emit(["up", "skill"], release_fallback=[])
                return self._emit(["up", "attack"], release_fallback=[])
            return self._emit(["forward"])

        return self._decide_neutral(obs)


def main():
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    bot = CombatBot()
    write = sys.stdout.write
    flush = sys.stdout.flush

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")
        req_id = msg.get("requestId")

        if msg_type == "draft":
            answer = draft_team(msg)
            answer["requestId"] = req_id
        elif msg_type == "frame":
            answer = {
                "type": "action",
                "requestId": req_id,
                "tick": safe_int(msg.get("tick"), 0),
                "buttons": bot.decide(msg),
            }
        else:
            continue

        write(json.dumps(answer, ensure_ascii=True, separators=(",", ":")) + "\n")
        flush()


if __name__ == "__main__":
    main()

