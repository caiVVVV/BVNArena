private typedef TrackedFighter = {
    var actor:Dynamic;
    var seat:Int;
    var round:Int;
    var hp:Float;
    var hurtHit:Dynamic;
    var died:Bool;
    var koSent:Bool;
}

/** Uses the native hit event and actual HP values, including the last hit of a round. */
class BattleTelemetry {
    final fighterEvents:Dynamic;
    final dispatcher:Dynamic;
    final publish:Dynamic->Void;
    final combatEvent:Dynamic->Void;
    final matchId:String;
    final switched:Bool;
    var listening:Array<String> = [];
    var fighters:Array<TrackedFighter> = [];
    var eventTick = 0;

    public function new(lookup:String->Dynamic, send:Dynamic->Void, matchId:String, switched:Bool) {
        fighterEvents = lookup("fighter.events::FighterEvent");
        dispatcher = lookup("fighter.events::FighterEventDispatcher");
        publish = send;
        this.matchId = matchId;
        this.switched = switched;
        combatEvent = event -> {
            // The game calls its listeners synchronously. A reporting error must
            // never interrupt native combat.
            try onCombatEvent(event) catch (_:Dynamic) {}
        };
    }

    public function listen():Void {
        if (fighterEvents == null || dispatcher == null) return;
        for (key in ["HIT_TARGET", "DO_ACTION", "DIE"]) {
            var eventName = stringValue(Reflect.field(fighterEvents, key));
            if (eventName != null) {
                dispatcher.addEventListener(eventName, combatEvent);
                listening.push(eventName);
            }
        }
    }

    public function close():Void {
        for (eventName in listening) dispatcher.removeEventListener(eventName, combatEvent);
        listening = [];
        fighters = [];
    }

    public function sample(tick:Int, round:Int, first:Dynamic, second:Dynamic):Void {
        eventTick = tick;
        for (entry in fighters) {
            var hit = lastHurtHit(entry.actor);
            // An unchanged hit record can belong to an earlier, unrelated HP loss.
            var source = hit != null && hit != entry.hurtHit ? hitSource(hit, null, entry) : null;
            observe(entry, source);
            if (entry.died) knockout(entry, source);
            entry.hurtHit = hit;
        }

        var next:Array<TrackedFighter> = [];
        var actors:Array<Dynamic> = [first, second];
        for (i in 0...2) {
            var fighter = actors[i];
            if (fighter == null) continue;
            var entry = tracked(fighter);
            if (entry == null) {
                entry = {actor:fighter, seat:switched ? 2-i : i+1, round:round,
                         hp:health(fighter), hurtHit:lastHurtHit(fighter), died:false, koSent:false};
            } else {
                entry.round = round;
            }
            next.push(entry);
        }
        fighters = next;
        eventTick = tick + 1;
    }

    function onCombatEvent(event:Dynamic):Void {
        if (event.type == fighterEvents.DIE) {
            var defeated = tracked(event.fighter);
            if (defeated != null) defeated.died = true;
            return;
        }
        if (event.params == null) return;
        if (event.type == fighterEvents.HIT_TARGET) {
            var target = tracked(event.params.target);
            var hit = event.params.hitvo;
            if (target != null && hit != null) {
                observe(target, hitSource(hit, event.fighter, target));
                target.hurtHit = lastHurtHit(target.actor);
            }
        } else if (event.type == fighterEvents.DO_ACTION) {
            var actor = tracked(event.fighter);
            var move = stringValue(event.params.action);
            if (move == null) move = stringValue(event.params.input);
            if (actor != null && move != null) publish({
                type:"attack", matchId:matchId, tick:eventTick, round:actor.round,
                seat:actor.seat, fighter:identity(actor.actor, actor.seat), move:move
            });
        }
    }

    function observe(target:TrackedFighter, source:Dynamic):Void {
        var before = target.hp;
        var after = health(target.actor);
        if (!validHealth(after)) return;
        target.hp = after;
        if (before <= after || !validHealth(before)) return;
        var who = identity(target.actor, target.seat);
        publish({type:"damage", matchId:matchId, tick:eventTick, round:target.round, target:who,
                 hpBefore:before, hpAfter:after, source:source});
        if (after == 0) knockout(target, source);
    }

    function knockout(target:TrackedFighter, source:Dynamic):Void {
        if (target.koSent) return;
        target.koSent = true;
        publish({type:"ko", matchId:matchId, tick:eventTick, round:target.round,
                 target:identity(target.actor, target.seat), source:source});
    }

    function hitSource(hit:Dynamic, attacker:Dynamic, target:TrackedFighter):Dynamic {
        var owner:Dynamic = hit.owner;
        var actor:Dynamic = owner != null ? owner : attacker;
        var sourceActor:Dynamic = actor;
        var kind = "fighter";
        for (_ in 0...5) {
            if (actor == null || tracked(actor) != null) break;
            var typeName:String = untyped __global__["flash.utils.getQualifiedClassName"](actor);
            if (StringTools.endsWith(typeName, "::Assister")) {
                kind = "assist";
                sourceActor = actor;
            } else if (StringTools.endsWith(typeName, "::Bullet") ||
                       StringTools.endsWith(typeName, "::FighterAttacker")) {
                if (kind != "assist") kind = "projectile";
            }
            if (!Reflect.isFunction(Reflect.field(actor, "getOwner"))) break;
            var parent:Dynamic = actor.getOwner();
            if (parent == actor) break;
            actor = parent;
        }
        var entry = tracked(actor);
        if (entry == null) entry = tracked(attacker);
        if (entry == null) return null;
        if (kind == "fighter" && entry.actor == target.actor) kind = "self";
        var source = identity(kind == "assist" ? sourceActor : entry.actor, entry.seat);
        return {kind:kind, seat:entry.seat, id:source.id, name:source.name,
                move:stringValue(hit.id), confirmed:true};
    }

    function tracked(actor:Dynamic):Null<TrackedFighter> {
        if (actor != null) for (entry in fighters) if (entry.actor == actor) return entry;
        return null;
    }

    static function lastHurtHit(actor:Dynamic):Dynamic {
        if (Reflect.isFunction(Reflect.field(actor, "getLastHurtHitVO"))) return actor.getLastHurtHitVO();
        return null;
    }

    static function identity(actor:Dynamic, seat:Int):Dynamic {
        var data = Reflect.field(actor, "data");
        return {seat:seat, id:data == null ? null : stringValue(data.id),
                name:data == null ? null : stringValue(data.name)};
    }

    static function health(actor:Dynamic):Float return Math.max(0, actor.hp);
    static function validHealth(value:Float):Bool return value >= 0 && value - value == 0;
    static function stringValue(value:Dynamic):String return Std.isOfType(value, String) && value != "" ? value : null;
}
