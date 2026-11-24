"""Install only arena-owned files into an existing BVN Sports directory."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile

BUILD = "3.8.8.5"
SUPPORTED_GAME_SHA256 = "5344e0b778da0b2d775f15ccf67e74753e53721a8a19e828b35455401c626396"
MANAGER = "AIArena/BVNArena.exe"
PLUGIN = "assets/plugin/ArenaBridge.swf"
MANIFEST = "AIArena/install.json"
ENTRANTS = "AIArena/Entrants"

# Concrete IDs from the supported 3.8.8.5 game's select menu, confirmed by its
# runtime catalog. Intersect these with installed assets before showing choices.
MENU_FIGHTERS = """
aizen aizen_captain byakuya chad deidara dordoni gaara gin grimmjow hinata
ichigo ichigo_zero ikkaku itachi jiraiya juggo kabuto kakashi kakashi_gsw kakashi_kamui
kenpachi kenshin killerbee kimimaro kimimaro_goninshu konan lee madara mayuri
naruto naruto_kyubi neji obito ogihci onoki orihime orochimaru pain renji rukia
sado_muerte sakura sakura_byakugo sasuke sasuke_susanoo soifon starrk suigetsu
temari tensa tobi toushirou ulquiorra uryuu yoruichi
""".split()
MENU_ASSISTS = """
aizen_muken bonnie byakuya choji debug fukasaku gaara gin grimmjow harribel_resurrection
hinata hiruko hiyori ichigo_dangai ikkaku_bankai ino isane itachi jiraiya kabuto karin
kenpachi kensei kimimaro kira kon madara minato naruto nel nemu obito ogihci_vasto_lord
onoki orihime rangiku renji riruka sai sakura_byakugo sasuke_amenotejikara shikamaru
shishio shunsui soifon starrk_resurrection suigetsu_taka tayuya temari tenten toushirou
tsunade urahara uryuu utakata yachiru yamamoto yoruichi zetsu
""".split()
PREFERRED_STARTERS = ("ichigo", "naruto", "sasuke")
PREFERRED_ASSIST = "kon"


class InstallError(ValueError):
    pass


def check_game(directory):
    root = Path(directory).resolve(strict=True)
    try:
        version = json.loads((root / "version.json").read_text(encoding="utf-8-sig"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallError("请选择包含 version.json 的竞技版游戏目录。") from exc
    executable = root / "Bleach vs. Naruto.exe"
    if version != BUILD or not executable.is_file():
        raise InstallError(f"此版本支持 Windows 竞技版 {BUILD}，所选目录版本为 {version}。")
    if digest(executable) != SUPPORTED_GAME_SHA256:
        raise InstallError(f"游戏 EXE 与竞技场支持的 Windows 竞技版 {BUILD} 构建不一致。")
    return root


def _game_names(root):
    try:
        lang = json.loads((root / "lang/zh-CN.json").read_text(encoding="utf-8-sig"))
        names = lang.get("name", lang)
        return names if isinstance(names, dict) else {}
    except (OSError, AttributeError, ValueError):
        return {}


def list_maps(root):
    labels = _game_names(root).get("map", {})
    if not isinstance(labels, dict):
        labels = {}
    return [(item.stem, labels.get(item.stem, item.stem))
            for item in sorted((root / "assets/map").glob("*.swf"))]


def selection_names(root):
    names = _game_names(root)
    labels = []
    for category in ("fighter", "assist"):
        entries = names.get(category)
        labels.append({key:value["name"] for key, value in entries.items()
                       if isinstance(value, dict) and isinstance(value.get("name"), str)}
                      if isinstance(entries, dict) else {})
    return labels


def installed_choices(root):
    fighters = root / "assets/fighter"
    assists = fighters / "fz"
    return ({p.stem for p in fighters.rglob("*.f") if p.is_file() and "fz" not in p.relative_to(fighters).parts},
            {p.stem for p in assists.rglob("*.f") if p.is_file()})


def list_human_choices(root):
    fighters, assists = installed_choices(root)
    fighter_names, assist_names = selection_names(root)
    return ([(i, fighter_names.get(i, i)) for i in MENU_FIGHTERS if i in fighters],
            [(i, assist_names.get(i, i)) for i in MENU_ASSISTS if i in assists])


def default_roster(fighters, team_size):
    preferred = [item for item in PREFERRED_STARTERS if item in fighters]
    return list(dict.fromkeys(preferred + list(fighters)))[:team_size]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def target(root, name):
    rel = PurePosixPath(name)
    if (name not in {MANAGER, PLUGIN, MANIFEST}
            and not (len(rel.parts) > 2 and rel.parts[:2] in {("AIArena", "SDK"), ("AIArena", "Entrants")})):
        raise InstallError("安装清单含有范围以外的路径。")
    if "\\" in name or ":" in name or ".." in rel.parts:
        raise InstallError("安装清单中的路径无效。")
    dest = root.joinpath(*rel.parts)
    if dest.resolve() != dest:
        raise InstallError(f"安装路径使用了重定向：{dest}")
    return dest


def read_manifest(root):
    path = target(root, MANIFEST)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("product") != "BVN Arena" or not isinstance(data.get("files"), dict):
        raise InstallError("现有安装清单无法识别。")
    for name in data["files"]:
        if name == MANIFEST:
            raise InstallError("安装清单损坏。")
        target(root, name)
    return data["files"]


def _manifest(files):
    return {"product": "BVN Arena", "format": 1, "game": BUILD, "files": files}


def deploy(directory, executable, payload):
    root = check_game(directory)
    payload, executable = Path(payload), Path(executable)
    files = {MANAGER: executable, PLUGIN: payload / "bridge/ArenaBridge.swf"}
    sdk = payload / "SDK"
    for src in sdk.rglob("*"):
        if src.is_file() and "__pycache__" not in src.parts and src.suffix != ".pyc":
            files["AIArena/SDK/" + src.relative_to(sdk).as_posix()] = src
    if len(files) <= 2:
        raise InstallError("安装包缺少选手开发包。")
    entrants = payload / "Entrants"
    for src in entrants.rglob("*"):
        if src.is_file() and "__pycache__" not in src.parts and src.suffix != ".pyc":
            files[f"{ENTRANTS}/" + src.relative_to(entrants).as_posix()] = src
    old = read_manifest(root)
    hashes = {name: expected for name, expected in old.items() if name.startswith(f"{ENTRANTS}/")}
    hashes.update((name, digest(src)) for name, src in files.items())
    changes = []
    for name in files:
        dest = target(root, name)
        if dest.exists():
            existing = digest(dest)
            if name not in old or existing != old[name]:
                raise InstallError(f"请先移开同名或已自行修改的文件：{dest}")
            if existing == hashes[name]:
                continue
        changes.append(name)
    stale = [name for name in old if name not in hashes and target(root, name).is_file()
             and digest(target(root, name)) == old[name]]
    manifest = _manifest(hashes)
    with tempfile.TemporaryDirectory(prefix=".bvn-arena-", dir=root) as temp:
        stage = Path(temp)
        for n, name in enumerate(changes):
            shutil.copyfile(files[name], stage / f"new{n}")
        manifest_index = len(changes)
        (stage / f"new{manifest_index}").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        applied = []
        try:
            for n, name in enumerate([*changes, MANIFEST, *stale]):
                dest = target(root, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                backup = stage / f"old{n}"
                if dest.exists():
                    os.replace(dest, backup)
                applied.append((dest, backup))
                if n <= manifest_index:
                    os.replace(stage / f"new{n}", dest)
        except OSError:
            for dest, backup in reversed(applied):
                if backup.exists():
                    os.replace(backup, dest)
                elif dest.exists():
                    dest.unlink()
            raise
    return root / MANAGER


def remove_installation(directory):
    root = Path(directory).resolve(strict=True)
    files = read_manifest(root)
    if not files:
        raise InstallError("该目录中没有可识别的竞技场安装。")
    kept = []
    empty_dirs = set()
    for name, expected in files.items():
        dest = target(root, name)
        if dest.is_file() and digest(dest) == expected:
            dest.unlink()
        elif dest.exists():
            kept.append(str(dest))
        if name.startswith("AIArena/"):
            empty_dirs.update(p for p in dest.parents if p.is_relative_to(root / "AIArena"))
    target(root, MANIFEST).unlink()
    for folder in sorted(empty_dirs, key=lambda p: -len(p.parts)):
        try:
            folder.rmdir()
        except OSError:
            pass
    return kept


def export_training_kit(directory, destination):
    root = check_game(directory)
    destination = Path(destination).resolve()
    if destination.is_relative_to(root):
        raise InstallError("请把训练包保存到游戏目录外。")
    owned = read_manifest(root)
    if not owned:
        raise InstallError("请先安装竞技场，再导出训练包。")
    training_files = {name: expected for name, expected in owned.items() if not name.startswith(f"{ENTRANTS}/")}
    files = {root / name for name in ("version.json", "Bleach vs. Naruto.exe", *training_files)}
    for folder in ("Adobe AIR", "assets", "META-INF", "lang"):
        files.update(p for p in (root / folder).rglob("*") if p.is_file() and p.resolve() == p)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for source in sorted(files):
                archive.write(source, "BVN/" + source.relative_to(root).as_posix())
            archive.writestr("BVN/" + MANIFEST, json.dumps(_manifest(training_files), ensure_ascii=False, indent=2))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
