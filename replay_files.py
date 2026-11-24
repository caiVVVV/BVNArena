"""Resolve a replay selection to the files the list manages."""
from pathlib import Path

from battle_report import SUFFIX as REPORT_SUFFIX


def files_for_selected_replays(directory, selected):
    root = Path(directory).resolve()
    files = set()
    for row in selected:
        path = Path(row).absolute()
        if (path.suffix.lower() not in {".bvnreplay", ".bvndemo"}
                or not path.is_relative_to(root) or path.resolve() != path):
            raise ValueError("只能删除竞技场回放目录中选中的比赛档案。")
        picture = path.with_suffix(".bvnreplay")
        demo = path.with_suffix(".bvndemo")
        report = path.with_suffix(REPORT_SUFFIX)
        journal = picture.with_name(picture.name + "-journal")
        for candidate in (path, picture, demo, report, journal):
            if candidate.is_symlink() or candidate.resolve() != candidate:
                raise ValueError("所选回放含重定向文件，请在文件管理器中处理。")
            if candidate.is_file():
                files.add(candidate)
    return sorted(files)
