# BVNArena · 死神vs火影 AI 竞技场

BVNArena 是一个 Windows 本地裁判和 AI 选手 SDK，适配《死神 vs 火影 竞技版》3.8.8.5 的指定构建。首发支持玩家用原版键位与自选的 AI 打一局三人小队赛，也可用同一套裁判运行 AI 对 AI 和 CPU 练习。

## 玩家对 AI

1. 在 B 站搜索 [死神VS火影 竞技版 3.8.8.5](https://search.bilibili.com/all?keyword=%E6%AD%BB%E7%A5%9EVS%E7%81%AB%E5%BD%B1%20%E7%AB%9E%E6%8A%80%E7%89%88%203.8.8.5)，查看官方账号 [死神VS火影吧](https://space.bilibili.com/1340107883) 的下载指引，确认下载的是 Windows 电脑版 3.8.8.5。解压后运行 `BVNArena-Setup.exe`，选择含 `version.json` 和原游戏 EXE 的目录。
2. 打开游戏目录下的 `AIArena/BVNArena.exe`，在「比赛模式」选择「玩家对 AI（单局）」。
3. 选择三名英雄的出场顺序、一个辅助和地图。从 P2 选一个带 `entrant.json` 的 AI 文件夹。安装后可先选 `AIArena/SDK/Examples` 中的任一样例。
4. 点击「开始比赛」。游戏窗口开启后，用原版 P1 键位操作。AI 通过裁判操作 P2。

在「回放」中可以看当时记录的画面，也可打开「赛后复盘」查看双方的实际扣血、回血和有证据支持的击倒来源。输入重演和跳过特写在界面中标为实验功能。

## 开发参赛 AI

- [SDK 入口](SDK/README.md)提供 Python 示例、选角流程和参赛清单格式。
- [JSONL 协议](SDK/协议.md)说明公开观测和按键。双方锁定选角后，裁判会提供对手的出场阵容、辅助和当前出场英雄。
- [裁判自动化](SDK/裁判自动化.md)说明如何通过 `--referee-api` 查询地图、启动 AI 对战或 CPU 练习、查询进度和停止任务。
- [赛后复盘](SDK/赛后复盘.md)说明 `.bvnreport.json` 的用法。[角色与辅助资料](SDK/角色与辅助数据.json)列出可选 ID 和已知机制。

## 在 Windows 上构建

需要 Python 3.12、Haxe 4.3、PowerShell 7 和 Windows 自带的 .NET Framework C# 编译器。将 Python 和 Haxe 加入 PATH 后，在源码根目录运行以下命令。

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
pwsh -File .\build.ps1
```

安装程序在 `dist/BVNArena-Setup.exe`。裁判会核对游戏版本与 EXE 指纹。

## 许可与归属

BVNArena 裁判、桥接器及 SDK 示例按 [GPL-3.0-or-later](LICENSE) 发布。桥接器使用原游戏提供的 SWF 插件入口。原游戏由 5DPLAY Game Studio 开发，使用者需自行准备游戏和运行环境。仓库提供 BVNArena 源码、文档和示例选手。[原游戏开源工程](https://github.com/5DPLAY-Game-Studio/BleachVsNaruto)
