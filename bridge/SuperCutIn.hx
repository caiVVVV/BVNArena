import flash.display.DisplayObjectContainer;

/** Ends a native super cut-in while leaving the move's fighter timeline intact. */
class SuperCutIn {
    final lookup:String->Dynamic;
    var effects:Dynamic;
    var assistType:Dynamic;
    var actionStates:Dynamic;
    var initialized=false;

    public function new(lookup:String->Dynamic) {
        this.lookup=lookup;
    }

    function initialize():Void {
        initialized=true;
        for (path in ["ctrl::EffectCtrl","ctrler::EffectCtrl"]) {
            var cls:Dynamic=resolve(path);
            if (has(cls,"I") && cls.I!=null && has(cls.I,"endBisha")) {
                effects=cls.I;
                break;
            }
        }
        assistType=resolve("fighter::Assister");
        actionStates=resolve("data.fighter::FighterActionState");
        if (actionStates==null) actionStates=resolve("fighter::FighterActionState");
    }

    function resolve(path:String):Dynamic {
        try return lookup(path) catch (_:Dynamic) return null;
    }

    static function has(o:Dynamic,name:String):Bool return o!=null && untyped __in__(name,o);

    function isSuper(fighter:Dynamic):Bool {
        if (fighter.actionState==12) return true;
        if (has(actionStates,"isBishaIng")) return actionStates.isBishaIng(fighter.actionState);
        return false;
    }

    function hasCutInBackdrop(stage:Dynamic):Bool {
        var display=Std.downcast(stage,DisplayObjectContainer);
        if (display==null && has(stage,"display")) display=Std.downcast(stage.display,DisplayObjectContainer);
        if (display==null) return false;
        for (i in 0...display.numChildren) {
            var child=display.getChildAt(i);
            if (!Std.isOfType(child,DisplayObjectContainer)) continue;
            var name:String=untyped __global__["flash.utils.getQualifiedClassName"](child);
            if (StringTools.endsWith(name,"::BlackBackView")) return true;
        }
        return false;
    }

    public function finishIfActive(stage:Dynamic, fighters:Array<Dynamic>):Bool {
        if (stage==null) return false;
        var map:Dynamic=stage.getMap();
        if (map==null || map.mapLayer==null || map.mapLayer.visible!=false) return false;
        if (!initialized) initialize();
        if (effects==null) return false;

        var casting=fighters.filter(fighter -> fighter!=null && isSuper(fighter));
        var sprites:Dynamic=stage.getGameSprites();
        var assists:Array<Dynamic>=[];
        for (i in 0...Std.int(sprites.length)) {
            var sprite:Dynamic=sprites[i];
            if (sprite!=null) {
                var name:String=untyped __global__["flash.utils.getQualifiedClassName"](sprite);
                if ((assistType!=null && Std.isOfType(sprite,assistType)) ||
                    StringTools.endsWith(name,"::Assister")) assists.push(sprite);
            }
        }
        // Some native assist cut-ins show a black backdrop without a portrait.
        if (assists.length>0 && hasCutInBackdrop(stage))
            casting=assists.concat(fighters.filter(fighter -> fighter!=null));
        for (caster in casting) {
            effects.endBisha(caster);
            if (map.mapLayer.visible) return true;
        }
        return false;
    }
}
