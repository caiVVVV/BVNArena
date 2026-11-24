"""Spacing and counterattack competitor, independent of the Blaze example."""
import json
import sys


def choose(query):
    pool = list(dict.fromkeys(query["fighters"]))
    pool.sort(key=lambda name: (name not in {"gaara", "byakuya", "temari", "itachi"}, name))
    return {"type": "pick", "roster": pool[:query["teamSize"]], "assist": query["assists"][-1]}


def defend(query, previous, phase):
    index = query["seat"] - 1
    me, opponent = query["players"][index], query["players"][1-index]
    gap = abs((opponent.get("x") or 0) - (me.get("x") or 0))
    if gap < 85 and previous - gap > 4:
        keys = ["back", "dash"]
    elif gap < 48:
        keys = ["down", "attack"] if phase % 3 else ["back", "jump"]
    elif gap > 205:
        keys = ["forward"]
    elif phase % 17 == 0:
        keys = ["assist"]
    elif phase % 9 == 0:
        keys = ["super"]
    elif phase % 3 == 0:
        keys = ["skill"]
    else:
        keys = ["back"] if gap < 124 else ["attack"]
    return gap, keys


def main():
    previous, phase = 0, 0
    for line in sys.stdin:
        query = json.loads(line)
        if query["type"] == "draft":
            answer = choose(query)
        elif query["type"] == "frame":
            phase += 1
            previous, keys = defend(query, previous, phase)
            answer = {"type":"action", "tick":query["tick"], "buttons":keys}
        else:
            continue
        answer["requestId"] = query["requestId"]
        print(json.dumps(answer, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
