"""Aggressive close-range competitor. Uses only the referee's public observation."""
import json
import sys


def draft(msg):
    fighters = msg["fighters"]
    preferred = ["lee", "kenpachi", "ichigo", "naruto", "sasuke"]
    ordered = list(dict.fromkeys([p for p in preferred if p in fighters] + fighters))
    return {"type": "pick", "roster": ordered[:msg["teamSize"]], "assist": msg["assists"][0]}


def fight(msg, turn):
    own = msg["seat"] - 1
    me, enemy = msg["players"][own], msg["players"][1-own]
    distance = abs((enemy.get("x") or 0) - (me.get("x") or 0))
    if distance > 165:
        return ["forward", "dash"] if turn % 5 == 0 else ["forward"]
    if distance > 69:
        return ["forward", "jump", "attack"] if turn % 11 == 0 else ["forward", "attack"]
    if turn % 19 == 0:
        return ["assist"]
    if turn % 13 == 0:
        return ["super"]
    if turn % 7 == 0:
        return ["down", "skill"]
    return ["attack"] if turn % 2 else ["forward"]


def main():
    turn = 0
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["type"] == "draft":
            output = draft(msg)
        elif msg["type"] == "frame":
            turn += 1
            output = {"type": "action", "tick": msg["tick"], "buttons": fight(msg, turn)}
        else:
            continue
        output["requestId"] = msg["requestId"]
        print(json.dumps(output, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
