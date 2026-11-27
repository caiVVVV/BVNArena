"""死神 vs 火影竞技版选手。输入和输出均为裁判协议 1 的 UTF-8 JSONL。"""
import json
import math
import sys


# 只在名单确实含有这些 ID 时使用偏好。未认识的角色保留裁判的顺序。
DRAFT_SLOTS = (
    ("pain", "itachi", "lee", "naruto"),
    ("aizen", "byakuya", "sasuke_susanoo", "ichigo"),
    ("madara", "gaara", "kenpachi", "temari"),
)
RANGED = {"madara", "sasuke_susanoo", "pain", "aizen", "itachi", "byakuya", "gaara", "temari"}
HIT_STATES = {"hurt", "hitstun", "stunned", "knockdown", "knockback", "受击", "倒地"}
ATTACK_STATES = {"attack", "attacking", "skill", "super", "攻击", "技能", "必杀"}
READY_STATES = {"0", "idle", "stand", "standing", "ready", "wait", "walk", "待机", "站立"}
SUPER_REACH = {"madara": 250, "itachi": 160, "pain": 117, "aizen": 110}


def pick(msg):
    pool = list(dict.fromkeys(msg["fighters"]))
    roster = []
    for choices in DRAFT_SLOTS:
        for fighter in choices:
            if fighter in pool and fighter not in roster:
                roster.append(fighter)
                break
    preferred = set(roster)
    roster.extend(fighter for fighter in pool if fighter not in preferred)
    allowed = msg["assists"]
    assist = next((x for x in ("ichigo_dangai", "aizen_muken", "yamamoto") if x in allowed), allowed[0])
    return {"type": "pick", "roster": roster[:msg["teamSize"]], "assist": assist}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


class Fighter:
    def __init__(self, data):
        self.id = data.get("id")
        self.name = data.get("name")
        self.x = number(data.get("x"))
        self.y = number(data.get("y"))
        self.facing = number(data.get("facing"))
        self.hp = number(data.get("hp"))
        maximum = number(data.get("hpMax"))
        self.health = self.hp / maximum if self.hp is not None and maximum is not None and maximum > 0 else None
        self.energy = number(data.get("energy"))
        self.qi = number(data.get("qi"))
        if self.energy is None:
            self.energy = number(data.get("qi"))
        self.state = str(data.get("state", "")).strip().lower()
        self.air = {"true": True, "false": False, "1": True, "0": False}.get(str(data.get("air")).lower())
        if self.air is None and data.get("air") is None and self.state in READY_STATES:
            self.air = False

    def lost_hp_since(self, previous):
        return self.hp is not None and previous.hp is not None and self.hp < previous.hp

    def reconcile_identity(self, old):
        # 不拿显示名和内部 ID 比较。间歇性缺字段时沿用各自最后一个已见值。
        if self.id is not None and old.id is not None:
            changed = self.id != old.id
        else:
            changed = self.name is not None and old.name is not None and self.name != old.name
        if not changed:
            if self.id is None:
                self.id = old.id
            if self.name is None:
                self.name = old.name
        return changed


class Tactics:
    def __init__(self):
        self.tick = None
        self.fighters = ()
        self.keys = []
        self.next_press = {}
        self.last_strike = None
        self.retreat_until = None
        self.jump_checked = False
        self.after_jump = None
        self.watch_rise_until = None
        self.commit_until = None

    def ready(self, key):
        return key not in self.keys and self.tick >= self.next_press.get(key, self.tick)

    def press(self, key, interval, direction=None):
        buttons = [direction] if direction else []
        if self.ready(key):
            self.next_press[key] = self.tick + interval
            buttons.append(key)
            if key in ("attack", "skill", "super"):
                self.last_strike = self.tick
            if key == "jump":
                self.after_jump = [direction] if direction else []
            if key in ("super", "assist"):
                self.commit_until = self.tick + 4
        return buttons

    def decide(self, me, enemy, previous, dt):
        if self.after_jump is not None:
            release = self.after_jump
            self.after_jump = None
            return release
        if self.watch_rise_until is not None:
            if me.air is False and self.tick <= self.watch_rise_until and me.state not in HIT_STATES:
                return []
            self.watch_rise_until = None
        # 公开 HP 会显示 0，但裁判仍可能下发可行动角色。换人和胜负由裁判判定。
        if me.x is None or enemy.x is None:
            return []

        dx = enemy.x - me.x
        gap = abs(dx)
        ranged = me.id in RANGED
        reach = 60 if ranged else 56
        skill_reach = 222 if ranged else 98
        approach = 0
        outward_speed = 0
        hurt = hit = enemy_finished = False
        if previous:
            old_me, old_enemy = previous
            hurt = me.lost_hp_since(old_me)
            hit = enemy.lost_hp_since(old_enemy)
            enemy_finished = (old_enemy.state in ATTACK_STATES or old_enemy.state in {"10", "11", "12"}) and enemy.state in READY_STATES
            if old_me.x is not None and old_enemy.x is not None:
                old_dx = old_enemy.x - old_me.x
                if old_dx * dx > 0:
                    # 对手向己方移动，且两人间距确实缩短。自己前冲不触发后撤。
                    enemy_step = (old_enemy.x - enemy.x) * (1 if dx > 0 else -1)
                    approach = min(enemy_step, abs(old_dx) - gap) / dt
                    outward_speed = -enemy_step / dt

        if hurt or me.state in HIT_STATES:
            self.retreat_until = self.tick + 5
        if self.commit_until is not None and self.tick < self.commit_until and approach < 3 and not hurt:
            return []
        retreating = self.retreat_until is not None and self.tick <= self.retreat_until
        grounded = me.air is False
        if (not self.jump_checked and self.ready("jump") and grounded and me.state in READY_STATES and gap > 195
                and not retreating and approach < 2 and enemy.state not in ATTACK_STATES):
            self.jump_checked = True
            self.watch_rise_until = self.tick + 10
            return self.press("jump", 30)

        high = enemy.y is not None and me.y is not None and me.y - enemy.y > 32
        aligned = me.y is None or enemy.y is None or abs(me.y - enemy.y) <= 58
        neutral = me.state in READY_STATES
        attacking = enemy.state in {"10", "11", "12"} or enemy.state in ATTACK_STATES
        if me.state in {"21", "22", "23", "24"}:
            # 受击与倒地阶段不把资源按键的重试窗口提前耗掉。
            return ["down"] if grounded else []
        if enemy.state in {"22", "23", "24"} and grounded:
            # 对手倒地或起身时留下气量，退到能接下一次进攻的位置。
            if gap < 76:
                return ["back"]
            return ["forward"] if gap > 125 else []
        charged = me.qi is not None and me.qi >= 100
        super_reach = SUPER_REACH.get(me.id, 185 if ranged else 70)
        buffer = neutral or me.state in {"16", "20", "40", "100"}
        faces_enemy = me.facing not in (-1, 1) or dx * me.facing >= 0
        same_height = me.y is None or enemy.y is None or abs(me.y - enemy.y) <= 34
        # 离得近但正在拉开的对手，等出招时可能已经超出有效范围。
        predicted_gap = gap + min(max(outward_speed, 0), 7) * 7
        targetable = enemy.state not in {"22", "23", "24", "100"}
        super_window = grounded and same_height and targetable and predicted_gap <= super_reach
        if charged and super_window and (buffer or hit):
            if not faces_enemy and gap > 18:
                return ["forward"]
            if self.ready("super"):
                return self.press("super", 9)
        # 观测到对手正在高速前冲时，提前腾出距离，让对手的冲刺先落空。
        if grounded and (neutral or me.state == "20") and enemy.state == "15" and approach >= 5:
            if gap - min(approach, 14) * 7 <= 84:
                if self.ready("dash") and (me.energy is None or me.energy >= 40):
                    return self.press("dash", 23, "back")
                return ["down"]
        if grounded and (neutral or me.state == "20") and enemy.state in {"11", "12"} and gap < 185 and self.ready("dash") and (me.energy is None or me.energy >= 40):
            return self.press("dash", 25, "back")
        if (grounded and (neutral or me.state == "20") and attacking and gap < 105
                and not hit and not (charged and super_window)):
            return ["down"]
        if buffer and grounded and aligned and reach < gap < 216 and not (hurt or hit) and self.ready("assist"):
            return self.press("assist", 95, "forward")
        if gap < 110 and (retreating or (approach > 3 and gap > reach)):
            return self.press("dash", 22, "back")
        if high and 65 < gap < 128 and grounded and not attacking and self.ready("jump"):
            return self.press("jump", 30, "forward")

        if me.health is not None and me.health < 0.32 and 55 <= gap <= 176 and neutral and self.ready("assist"):
            return self.press("assist", 135)

        recent_strike = self.last_strike is not None and self.tick - self.last_strike <= 12
        if gap <= skill_reach and hit and recent_strike:
            # 各角色的资源量纲可能不同，只在有能量且看到终结机会时试探必杀。
            if (super_window and faces_enemy
                    and (charged or (me.qi is None and me.energy is not None and me.energy > 0)) and enemy.health is not None
                    and enemy.health < 0.34 and self.ready("super")):
                return self.press("super", 80)
            if self.ready("skill"):
                return self.press("skill", 16)

        if enemy_finished and not hurt and gap <= skill_reach and self.ready("skill"):
            return self.press("skill", 18)
        if ranged and neutral and grounded and not hit and 50 < gap < 118 and approach > 3 and self.ready("dash"):
            return self.press("dash", 22, "back")
        if gap <= reach:
            if ranged and buffer and self.ready("skill"):
                return self.press("skill", 9, "up" if high else "down")
            if enemy.state in ATTACK_STATES and not hit and self.ready("assist"):
                return self.press("assist", 135, "back")
            return self.press("attack", 3, "forward" if me.air is True else None)
        if gap <= skill_reach and self.ready("skill"):
            return self.press("skill", 10, "up" if high else "forward")
        if ranged and grounded and 95 < gap < 150 and attacking and approach > 0:
            return ["back"]
        if gap > 240 and me.air is not True and (me.energy is None or me.energy >= 40):
            return self.press("dash", 24, "forward")
        return ["forward"]

    def frame(self, msg):
        seat = msg["seat"] - 1
        players = msg["players"]
        pair = (Fighter(players[seat] or {}), Fighter(players[1-seat] or {}))
        tick = msg["tick"]
        if self.tick is not None and tick <= self.tick:
            # 重复观测维持按键；过时请求回空，保留最新观测历史。
            return self.keys if tick == self.tick else []

        swapped = [current.reconcile_identity(old) for current, old in zip(pair, self.fighters)]
        dt = tick - self.tick if self.tick is not None else 0
        previous = self.fighters if 0 < dt <= 12 else ()
        if any(swapped) or not previous:
            previous = ()
            self.last_strike = self.retreat_until = None
        if any(swapped):
            self.after_jump = None
            self.watch_rise_until = None
            self.commit_until = None
            if swapped[0]:
                self.next_press = {key: due for key, due in self.next_press.items() if key == "assist"}
                self.jump_checked = False
        self.fighters = pair
        self.tick = tick
        self.keys = self.decide(pair[0], pair[1], previous, dt)
        return self.keys


def main():
    tactics = Tactics()
    for line in sys.stdin.buffer:
        msg = json.loads(line.decode("utf-8"))
        if msg["type"] == "draft":
            tactics = Tactics()
            answer = pick(msg)
        elif msg["type"] == "frame":
            answer = {"type": "action", "tick": msg["tick"], "buttons": tactics.frame(msg)}
        else:
            continue
        answer["requestId"] = msg["requestId"]
        wire = json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(wire + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
