import flash.display.DisplayObject;
import flash.display.MovieClip;
import flash.display.Stage;
import flash.events.Event;
import flash.events.IOErrorEvent;
import flash.events.ProgressEvent;
import flash.events.SecurityErrorEvent;
import flash.net.Socket;
import flash.system.ApplicationDomain;
import flash.utils.ByteArray;
import flash.utils.Timer;
import flash.events.TimerEvent;
import flash.events.KeyboardEvent;
import flash.display.Sprite;
import flash.text.TextField;
import flash.text.TextFormat;
import haxe.Json;

/** Entry point exported under the same name as the game's energy.fla example. */
@:keep class PluginMain extends MovieClip {
    public var CONFIG:Dynamic;
    public var location:MovieClip;
    var mainStage:Stage;
    var gameRender:Dynamic;
    var socket:Socket;
    var inputLine = new ByteArray();
    var retry:Timer;
    var tick = 0;
    var connected = false;
    var attempting = false;
    var lastDiagnostic = "";
    var ctl:Dynamic;
    var data:Dynamic;
    var mainGame:Dynamic;
    var events:Dynamic;
    var keys:Dynamic;
    var keyTypes:Dynamic;
    var setup:Dynamic;
    var active = false;
    var catalogSent = false;
    var fids:Array<String> = [];
    var aids:Array<String> = [];
    var mids:Array<String> = [];
    var held:Array<Int> = [];
    var lastInput = -1;
    var inputTime = 0;
    var firstFrame = false;
    var ordered = false;
    var labelLayer:Sprite;
    var replayBadge:TextField;
    var timingSaved=false;
    var savedTime:Int;
    var savedCpu:Bool;
    var switched=false;
    var pending:Dynamic;
    var tapeId:String;
    var tape=new Map<Int,Dynamic>();
    var originTick=-1;
    var firstCaptured=-1;
    var superCutIn:SuperCutIn;
    var cutInError=false;
    var labelError:String;
    var labelStep:String;
    var telemetry:BattleTelemetry;
    var telemetryError=false;
    var humanKeys:Array<String> = [];
    var aiKeys:Array<String> = [];
    var aiApplied:Array<String> = [];
    var sharedKeyWarning=false;
    static final BUTTONS=["left","right","up","down","attack","jump","skill","dash","super","assist"];

    public function new() {
        super();
        CONFIG = {id:"codex_arena_bridge", name:"AI 对战裁判", author:"Codex", isRunBack:true, isNeedDebug:false};
        location = new MovieClip();
        location.graphics.beginFill(0, 0);
        location.graphics.drawRect(0, 0, 1, 1);
        location.graphics.endFill();
        addChild(location);
    }

    public function initPlugin(?params:Dynamic):Void {
        if (params == null || mainStage != null) return;
        mainStage = params.mainStage;
        gameRender = params.gameRender;
        retry = new Timer(1500);
        retry.addEventListener(TimerEvent.TIMER, keepConnected);
        retry.start();
        keepConnected(null);
        gameRender.addAfter(render);
    }

    function keepConnected(event:Event):Void {
        if (attempting || connected) return;
        attempting = true;
        socket = new Socket();
        socket.addEventListener(Event.CONNECT, onConnect);
        socket.addEventListener(Event.CLOSE, onClose);
        socket.addEventListener(IOErrorEvent.IO_ERROR, onClose);
        socket.addEventListener(SecurityErrorEvent.SECURITY_ERROR, onClose);
        socket.addEventListener(ProgressEvent.SOCKET_DATA, onData);
        try socket.connect("127.0.0.1",17638) catch (error:Dynamic) onClose(null);
    }

    function onConnect(event:Event):Void {
        attempting = false;
        connected = true;
        inputLine.clear();
        send({type:"hello",protocol:1});
        status("BVN official plugin initialized");
        try {
            ctl = lookup("ctrl.game_ctrls::GameCtrl").I;
            data = lookup("data::GameData").I;
            mainGame = lookup("::MainGame");
            keys = lookup("input::GameInputer");
            keyTypes = lookup("input::GameInputType");
            if (events==null) {
                events = lookup("events::GameEvent");
                events.addEventListener(events.GAME_END, onGameEnd);
            }
            catalogSent = false;
            catalog();
        } catch (e:Dynamic) status("initialization: "+Std.string(e));
    }

    function onClose(event:Event):Void {
        releaseKeys();
        restoreConfig();
        active=false;
        closeTelemetry();
        setup=null;
        pending=null;
        clearLabels();
        clearReplayBadge();
        attempting = false;
        connected = false;
        inputLine.clear();
    }

    function send(message:Dynamic):Void {
        if (!connected) return;
        try {
            socket.writeUTFBytes(Json.stringify(message) + "\n");
            socket.flush();
        } catch (error:Dynamic) onClose(null);
    }
    function status(message:String):Void send({type:"status",message:message});

    function onData(event:ProgressEvent):Void {
        while (socket.bytesAvailable > 0) {
            var b = socket.readUnsignedByte();
            if (b != 10) {
                if (inputLine.length > 65536) { socket.close(); onClose(null); return; }
                inputLine.writeByte(b);
                continue;
            }
            inputLine.position = 0;
            var line = inputLine.readUTFBytes(inputLine.length);
            inputLine.clear();
            try command(Json.parse(line)) catch(e:Dynamic) status("command: " + Std.string(e));
        }
    }

    function gameClass(path:String):Dynamic {
        var d = ApplicationDomain.currentDomain;
        if (d.hasDefinition(path)) return d.getDefinition(path);
        if (!d.hasDefinition("GetGameClass")) return null;
        var resolve:Dynamic = d.getDefinition("GetGameClass");
        try return resolve(path) catch (_:Dynamic) return null;
    }

    function has(o:Dynamic,key:String):Bool return o!=null && untyped __in__(key,o);
    function lookup(suffix:String):Dynamic return gameClass("net.play5d.game.bvn"+(suffix.charAt(0)==":" ? "" : ".")+suffix);
    function catalog():Void {
        var fighters:Dynamic=lookup("data::FighterModel").I.getAllFighters();
        var assists:Dynamic=lookup("data::AssisterModel").I.getAllAssisters();
        var maps:Dynamic=lookup("data::MapModel").I.getAllMaps();
        if (fighters==null || assists==null || maps==null) return;
        var selectConfig=data.config.select_config;
        if (selectConfig==null || selectConfig.charList==null || selectConfig.assistList==null) return;
        fids = menuIds(selectConfig.charList, fighters);
        aids = menuIds(selectConfig.assistList, assists);
        mids = collectIds(maps).filter(concreteId);
        if (fids.length<3 || aids.length==0 || mids.length==0) return;
        send({type:"catalog",fighters:fids,assists:aids,maps:mids,teamSize:3});
        catalogSent=true;
    }
    function concreteId(id:String):Bool return id!="random" && !StringTools.startsWith(id,"random_");
    function menuIds(menu:Dynamic, all:Dynamic):Array<String> {
        var result:Array<String>=[];
        var items:Array<Dynamic>=menu.list;
        for (item in items) {
            if (item==null) continue;
            var ids:Array<Dynamic>=[item.fighterID];
            if (has(item,"moreFighterIDs") && item.moreFighterIDs!=null) ids=ids.concat(item.moreFighterIDs);
            for (value in ids) {
                if (value==null) continue;
                var id=Std.string(value);
                if (concreteId(id) && has(all,id) && result.indexOf(id)<0) result.push(id);
            }
        }
        result.sort(Reflect.compare);
        return result;
    }
    function collectIds(entries:Dynamic):Array<String> {
        var found:Array<String>=[];
        for (k in Reflect.fields(entries)) {
            var item=Reflect.field(entries,k);
            if (item!=null && has(item,"id") && item.id!=null && found.indexOf(item.id)<0) found.push(item.id);
        }
        found.sort(Reflect.compare);
        return found;
    }
    function select(side:Dynamic):Dynamic {
        var roster:Array<Dynamic>=side.roster;
        if (roster==null || roster.length!=3 || aids.indexOf(side.assist)<0) throw "invalid team or assist";
        for (id in roster) if (fids.indexOf(id)<0) throw "unknown fighter "+Std.string(id);
        var obj:Dynamic=Type.createInstance(cast lookup("data::SelectVO"),[]);
        obj.fighter1=roster[0]; obj.fighter2=roster[1]; obj.fighter3=roster[2]; obj.fuzhu=side.assist;
        return obj;
    }
    function command(msg:Dynamic):Void {
        switch (msg.type) {
            case "tape":
                if (!Std.isOfType(msg.matchId,String) || (active && setup.matchId==msg.matchId)) return;
                if (tapeId!=msg.matchId) {tapeId=msg.matchId;tape=new Map();}
                var entries:Array<Dynamic>=msg.events;
                for (entry in entries) {
                    var offset=Std.int(entry.offset);
                    if (offset<0 || offset>5000000) throw "invalid tape offset";
                    tape.set(offset,entry);
                }
            case "setup":
                if (!Std.isOfType(msg.matchId,String) || msg.matchId=="") throw "missing matchId";
                if (!catalogSent || mids.indexOf(msg.map)<0) throw "unknown map";
                if (msg.replay==true && tapeId!=msg.matchId) throw "no replay tape";
                if (msg.replay!=true) {tape=new Map();tapeId=null;}
                switched=msg.p1.practiceCPU==true;
                if (switched && msg.p2.practiceCPU==true) throw "at most one CPU";
                var s1=select(switched ? msg.p2 : msg.p1), s2=select(switched ? msg.p1 : msg.p2);
                releaseKeys();
                var mode=lookup("data::GameMode");
                if (!timingSaved) { savedTime=data.config.fightTime;savedCpu=data.config.enableCurAI;timingSaved=true; }
                // Record the rule in new demos. Older setups used a 60-second round.
                data.config.fightTime=msg.fightTime==-1 ? -1 : 60;
                if (switched || msg.p2.practiceCPU==true) data.config.enableCurAI=false;
                mode.currentMode=(switched || msg.p2.practiceCPU==true) ? mode.TEAM_VS_CPU : mode.TEAM_VS_PEOPLE;
                data.p1Select=s1; data.p2Select=s2; data.selectMap=msg.map;
                setup=msg; firstFrame=false; ordered=false; active=true; lastInput=-1;
                pending=null;originTick=-1;firstCaptured=-1;labelError=null;
                closeTelemetry();
                telemetryError=false;
                humanKeys=[]; aiKeys=[]; aiApplied=[]; sharedKeyWarning=false;
                superCutIn=msg.skipCutIn==true ? new SuperCutIn(lookup) : null;
                cutInError=false;
                clearLabels();
                clearReplayBadge();
                if (msg.replay==true) showReplayBadge();
                mainGame.I.loadGame();
                status("Loading "+msg.map);
            case "reset":
                if (setup==null || msg.matchId!=setup.matchId) return;
                active=false;
                releaseKeys(); clearLabels();
                clearReplayBadge();
                superCutIn=null;
                closeTelemetry();
                restoreConfig();pending=null;setup=null;lastInput=-1;
            case "input":
                if (active && setup.replay!=true && msg.matchId==setup.matchId && ctl.actionEnable==true &&
                    msg.tick>=firstCaptured && msg.tick<=tick && msg.tick>=tick-8 && msg.tick>lastInput &&
                    (pending==null || msg.tick>pending.tick)) pending=msg;
        }
    }
    function restoreConfig():Void {
        if (!timingSaved) return;
        data.config.fightTime=savedTime;
        data.config.enableCurAI=savedCpu;
        timingSaved=false;
    }
    function sideForSeat(i:Int):Dynamic return (i==0)!=switched ? setup.p1 : setup.p2;
    function humanPlaying():Bool return setup.replay!=true && setup.p1.human==true;

    function closeTelemetry():Void {
        if (telemetry==null) return;
        telemetry.close();
        telemetry=null;
    }

    function sampleTelemetry(run:Dynamic,a:Dynamic,b:Dynamic):Void {
        if (setup.replay==true || telemetryError || run==null) return;
        try {
            if (telemetry==null) {
                telemetry=new BattleTelemetry(lookup,send,setup.matchId,switched);
                telemetry.listen();
            }
            telemetry.sample(tick,run.round,a,b);
        } catch (error:Dynamic) {
            closeTelemetry();
            telemetryError=true;
            status("战斗事件记录中断："+Std.string(error));
        }
    }

    function render():Void {
        tick++;
        if (!connected || ctl==null) return;
        try {
            if (replayBadge!=null) {
                if (replayBadge.parent!=mainStage) mainStage.addChild(replayBadge);
                else if (mainStage.getChildIndex(replayBadge)!=mainStage.numChildren-1)
                    mainStage.setChildIndex(replayBadge,mainStage.numChildren-1);
            }
            if (!catalogSent && tick%20==0) catalog();
            if (!active) return;
            var stageNow=mainGame.stageCtrl.currentStage;
            if (stageNow!=null && has(stageNow,"setOrder") && !ordered) {
                stageNow.setOrder(1,sideForSeat(0).roster);
                stageNow.setOrder(2,sideForSeat(1).roster);
                ordered=true;
            }
            var run:Dynamic=ctl.gameRunData;
            var a:Dynamic=run!=null && run.p1FighterGroup!=null ? run.p1FighterGroup.currentFighter : null;
            var b:Dynamic=run!=null && run.p2FighterGroup!=null ? run.p2FighterGroup.currentFighter : null;
            if (firstFrame) sampleTelemetry(run,a,b);
            if (a==null || b==null || ctl.actionEnable!=true) {
                if (firstFrame) updateLabels(a,b);
                releaseKeys();pending=null; aiKeys=[]; aiApplied=[];
                return;
            }
            if (!firstFrame) {
                status("Fight begins: "+a.data.id+" vs "+b.data.id);
                firstFrame=true;
                originTick=tick;firstCaptured=tick;
                sampleTelemetry(run,a,b);
            }
            if (superCutIn!=null && !cutInError) {
                try {
                    var fighters:Array<Dynamic>=[a,b];
                    if (superCutIn.finishIfActive(ctl.gameState,fighters))
                        send({type:"cutInSkipped",matchId:setup.matchId,tick:tick});
                } catch (error:Dynamic) {
                    cutInError=true;
                    status("本局无法跳过大招特写："+Std.string(error));
                }
            }
            updateLabels(a,b);
            if (held.length>0 && tick-inputTime>12) {
                releaseKeys();
                aiKeys=[]; aiApplied=[];
            }
            if ((tick-originTick)%2==0) {
                var players=switched ? [player(b),player(a)] : [player(a),player(b)];
                send({type:"frame",matchId:setup.matchId,tick:tick,round:run.round,time:run.gameTime,map:data.selectMap,players:players});
            }
            if (setup.replay==true) {
                var event=tape.get(tick-originTick);
                if (event!=null) applyInput({tick:tick,p1:event.p1,p2:event.p2});
            } else {
                var input=pending;pending=null;
                if (input!=null && input.tick>=tick-8) applyInput(input);
                else if (humanPlaying()) recordHumanChange();
            }
        } catch(e:Dynamic) {
            var message=Std.string(e);
            if (message!=lastDiagnostic || tick%180==0) {status("render: "+message);lastDiagnostic=message;}
        }
    }
    function player(f:Dynamic):Dynamic {
        return {id:f.data.id,name:f.data.name,x:f.x,y:f.y,hp:f.hp,hpMax:f.hpMax,energy:f.energy,qi:f.qi,
                facing:f.direct,state:f.actionState,air:f.isInAir};
    }
    function onGameEnd(event:Dynamic):Void {
        if (!active || !firstFrame) return;
        var run=ctl.gameRunData;
        sampleTelemetry(run,run.p1FighterGroup.currentFighter,run.p2FighterGroup.currentFighter);
        closeTelemetry();
        var team:Dynamic=run.lastWinnerTeam;
        var winner:Int=team!=null ? Std.int(team.id) : 0;
        if (winner<0 || winner>2) {status("Unknown winning team");return;}
        if (switched && winner!=0) winner=3-winner;
        active=false;releaseKeys();restoreConfig();
        clearLabels();
        send({type:"result",matchId:setup.matchId,winner:winner,tick:tick});
    }
    function buttonKey(button:String, f:Dynamic, opponent:Dynamic):String {
        var towardRight=opponent==null || f.x==opponent.x ? f.direct>=0 : opponent.x>f.x;
        return switch button {
            case "forward": towardRight ? "right" : "left";
            case "back": towardRight ? "left" : "right";
            case "super":"superSkill";
            case "assist":"assist";
            case "left","right","up","down","attack","jump","skill","dash":button;
            default:"";
        };
    }
    function applyInput(msg:Dynamic):Void {
        var next:Array<Int>=[];
        var sides:Array<Array<String>>=[[],[]];
        var run=ctl.gameRunData;
        var human=humanPlaying();
        for (side in 0...2) {
            var competitor=sideForSeat(side);
            if (competitor.practiceCPU==true || (human && side==0)) continue;
            var requested:Array<Dynamic>=(side==0)!=switched ? msg.p1 : msg.p2;
            var cfg:Dynamic=side==0 ? data.config.key_p1 : data.config.key_p2;
            var f=side==0 ? run.p1FighterGroup.currentFighter : run.p2FighterGroup.currentFighter;
            var opponent=side==0 ? run.p2FighterGroup.currentFighter : run.p1FighterGroup.currentFighter;
            if (requested==null || f==null) continue;
            for (value in requested) {
                var button:String=Std.string(value);
                var name=buttonKey(button,f,opponent);
                if (name=="" || !has(cfg,name)) continue;
                var code:Int=Std.int(Reflect.field(cfg,name));
                if (human && sharedHumanKey(code)) {
                    if (!sharedKeyWarning) status("玩家与 P2 的键位有重复。已跳过冲突键，请在原版键位设置中调整。");
                    sharedKeyWarning=true;
                    continue;
                }
                if (code>0 && next.indexOf(code)<0) next.push(code);
                if (code>0 && sides[side].indexOf(button)<0) sides[side].push(button);
            }
        }
        for (code in held) if (next.indexOf(code)<0) dispatchKey(code,false);
        for (code in next) if (held.indexOf(code)<0) dispatchKey(code,true);
        held=next;
        inputTime=tick;
        lastInput=Std.int(msg.tick);
        var applied:Array<Array<String>>=[[],[]];
        var physical:Array<Array<String>>=[[],[]];
        for (side in 0...2) {
            var f=side==0 ? run.p1FighterGroup.currentFighter : run.p2FighterGroup.currentFighter;
            var opponent=side==0 ? run.p2FighterGroup.currentFighter : run.p1FighterGroup.currentFighter;
            var keyType=side==0 ? keyTypes.P1 : keyTypes.P2;
            for (button in sides[side]) {
                var field=buttonKey(button,f,opponent);
                if (field=="assist") field="special";
                var concrete=(button=="forward" || button=="back") ? field : button;
                if (physical[side].indexOf(concrete)<0) physical[side].push(concrete);
                if (Reflect.callMethod(keys,Reflect.field(keys,field),[keyType])==true) {
                    applied[side].push(button);
                }
            }
        }
        if (human) {
            humanKeys=readHumanKeys();
            aiApplied=applied[1];
            aiKeys=physical[1];
            applied[0]=humanKeys;
            physical[0]=humanKeys;
        }
        var one=switched?1:0;var two=1-one;
        send({type:"applied",matchId:setup.matchId,tick:tick,requestedTick:lastInput,p1:applied[one],p2:applied[two],keys1:physical[one],keys2:physical[two]});
    }

    function sharedHumanKey(code:Int):Bool {
        if (code<=0) return false;
        var cfg=data.config.key_p1;
        for (button in BUTTONS) {
            var field=button=="super" ? "superSkill" : button;
            if (Std.int(Reflect.field(cfg,field))==code) return true;
        }
        return false;
    }

    function readHumanKeys():Array<String> {
        return BUTTONS.filter(button -> {
            var field=switch (button) {
                case "super": "superSkill";
                case "assist": "special";
                default: button;
            };
            var fun=Reflect.field(keys,field);
            return Reflect.isFunction(fun) && Reflect.callMethod(keys,fun,[keyTypes.P1])==true;
        });
    }

    function recordHumanChange():Void {
        var current=readHumanKeys();
        if (current.length==humanKeys.length && current.join(",")==humanKeys.join(",")) return;
        humanKeys=current;
        send({type:"applied",matchId:setup.matchId,tick:tick,requestedTick:lastInput,
              p1:humanKeys,p2:aiApplied,keys1:humanKeys,keys2:aiKeys});
    }
    function dispatchKey(code:Int,down:Bool):Void {
        mainStage.dispatchEvent(new KeyboardEvent(down?KeyboardEvent.KEY_DOWN:KeyboardEvent.KEY_UP,true,false,0,code));
    }
    function releaseKeys():Void {
        for (code in held) dispatchKey(code,false);
        held=[];
    }
    function clearLabels():Void {
        if (labelLayer!=null && labelLayer.parent!=null) labelLayer.parent.removeChild(labelLayer);
        labelLayer=null;
        nameplates=[];
    }
    function showReplayBadge():Void {
        var field=new TextField();
        var format=new TextFormat("Microsoft YaHei",15,0xFFE5A1,true);
        format.align=flash.text.TextFormatAlign.CENTER;
        field.defaultTextFormat=format;
        field.text="回放 · 游戏重演";
        field.width=132;
        field.height=25;
        field.y=73;
        field.background=true;
        field.backgroundColor=0x171920;
        field.selectable=false;
        field.mouseEnabled=false;
        replayBadge=field;
        mainStage.addChild(field);
        centerReplayBadge(null);
        mainStage.addEventListener(Event.RESIZE,centerReplayBadge);
    }
    function centerReplayBadge(event:Event):Void {
        if (replayBadge!=null) replayBadge.x=Math.max(0,(mainStage.stageWidth-replayBadge.width)/2);
    }
    function clearReplayBadge():Void {
        if (replayBadge==null) return;
        mainStage.removeEventListener(Event.RESIZE,centerReplayBadge);
        if (replayBadge.parent!=null) replayBadge.parent.removeChild(replayBadge);
        replayBadge=null;
    }
    var nameplates:Array<TextField> = [];
    function labels():Void {
        clearLabels();
        labelStep="stage";
        labelLayer=new Sprite(); labelLayer.mouseEnabled=false;labelLayer.mouseChildren=false;
        mainStage.addChild(labelLayer);
        for (i in 0...2) {
            labelStep="create";
            var side:Dynamic=sideForSeat(i);
            var field=new TextField(); field.selectable=false;field.mouseEnabled=false;
            labelStep="color";
            var color:Int=side.colorValue==null ? (i==0 ? 0x63a9ff : 0xff8d7b) : side.colorValue;
            labelStep="format";
            var fmt=new TextFormat("Microsoft YaHei",15,color,true);
            fmt.align=i==0?flash.text.TextFormatAlign.LEFT:flash.text.TextFormatAlign.RIGHT;
            field.defaultTextFormat=fmt;field.width=220;field.height=24;
            field.background=true;field.backgroundColor=0x171920;
            var logical=switched ? 1-i : i;
            labelStep="score";
            field.text="P"+(logical+1)+"  "+Std.string(side.name)+"  "+Std.string(setup.score[logical]);
            field.x=i==0 ? 8 : mainStage.stageWidth-228;field.y=73;
            labelLayer.addChild(field);
            labelStep="name";
            var plate=new TextField();
            plate.defaultTextFormat=new TextFormat("Microsoft YaHei",14,color,true);
            plate.autoSize=flash.text.TextFieldAutoSize.LEFT;
            plate.background=true; plate.backgroundColor=0x171920;
            plate.selectable=false; plate.mouseEnabled=false;
            plate.visible=false;
            plate.text="  "+Std.string(side.name)+"  ";
            labelLayer.addChild(plate);
            nameplates.push(plate);
        }
        status("Nameplates attached");
    }
    function updateLabels(a:Dynamic,b:Dynamic):Void {
        try {
            if (labelLayer==null && labelError==null) labels();
            if (labelLayer!=null && labelLayer.parent!=mainStage) mainStage.addChild(labelLayer);
            moveLabels(a,b);
        } catch (error:Dynamic) {
            var message=Std.string(error);
            if (labelError!=message) {
                labelError=message;
                status("Nameplates "+labelStep+": "+message);
            }
        }
    }
    function displayForPlate(fighter:Dynamic):DisplayObject {
        if (fighter==null || fighter.hp<=0) return null;
        var display:DisplayObject=null;
        try display=fighter.getDisplay() catch (_:Dynamic) {}
        if (display==null || display.stage!=mainStage) return null;
        var ancestor=display;
        while (ancestor!=null && ancestor!=mainStage) {
            if (!ancestor.visible || ancestor.alpha<=0) return null;
            ancestor=ancestor.parent;
        }
        return display;
    }
    function moveLabels(a:Dynamic,b:Dynamic):Void {
        if (labelLayer==null || nameplates.length!=2) return;
        labelStep="position";
        var fighters=[a,b];
        for (i in 0...2) {
            var plate=nameplates[i];
            plate.visible=false;
            var display=displayForPlate(fighters[i]);
            if (display==null) continue;
            var bounds=display.getBounds(labelLayer);
            if (bounds.width<=0 || bounds.height<=0 || bounds.right<=0 || bounds.left>=mainStage.stageWidth ||
                bounds.bottom<=0 || bounds.top>=mainStage.stageHeight) continue;
            plate.x=Math.max(2,Math.min(mainStage.stageWidth-2-plate.width,bounds.x+(bounds.width-plate.width)/2));
            plate.y=Math.max(98,Math.min(mainStage.stageHeight-26,bounds.y-plate.height-4));
            plate.visible=true;
        }
        if (nameplates[0].visible && nameplates[1].visible &&
             nameplates[0].getBounds(labelLayer).intersects(nameplates[1].getBounds(labelLayer))) {
            var above=nameplates[0].y>124;
            nameplates[1].y=above ? nameplates[0].y-nameplates[1].height-3 : nameplates[0].y+nameplates[0].height+3;
        }
    }

    public function destroy():Void {
        releaseKeys();restoreConfig();clearLabels();
        closeTelemetry();
        clearReplayBadge();
        if (events!=null) events.removeEventListener(events.GAME_END,onGameEnd);
        if (retry != null) retry.stop();
        if (gameRender != null) gameRender.removeAfter(render);
        if (socket != null && socket.connected) socket.close();
        connected = false;
    }

    static function main():Void {}
}
