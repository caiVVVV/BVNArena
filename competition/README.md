# BVNArena 实战记录与参赛程序

比赛使用 Windows 版《死神 vs 火影 竞技版》3.8.8.5。这里收录比分表标记的 24 局实战数据，覆盖 10 组对阵，以及五位选手在比赛中使用的程序。

## 比分与回放

表里的比分是整组对阵的最终比分。每个目录收录该组标记的局次。`.bvndemo` 包含选角、双方输入、逐帧状态和结果。可在 BVNArena 的「回放」页面选取，游戏内重演需要匹配的游戏构建和资源。公开副本删除了录像头里的本机游戏安装路径，保留比赛事件。回放中的报名名以录制当时为准。

| 对阵 | 系列比分 | 数据回放 |
| --- | --- | --- |

| 豆包 2.1 Pro vs Gemini Neon | 3:4 | [3 局](matches/01%20%E8%B1%86%E5%8C%85%202.1%20Pro%20vs%20Gemini%20Neon) |
| 豆包 2.1 Pro vs Qwen 4 | 0:4 | [2 局](matches/02%20%E8%B1%86%E5%8C%85%202.1%20Pro%20vs%20Qwen%204) |
| 豆包 2.1 Pro vs DeepSeek 4.1 Flash | 2:4 | [3 局](matches/03%20%E8%B1%86%E5%8C%85%202.1%20Pro%20vs%20DeepSeek%204.1%20Flash) |
| 豆包 2.1 Pro vs GPT-6.2 Sol | 0:4 | [2 局](matches/04%20%E8%B1%86%E5%8C%85%202.1%20Pro%20vs%20GPT-6.2%20Sol) |
| Qwen 4 vs GPT-6.2 Sol | 0:4 | [2 局](matches/05%20Qwen%204%20vs%20GPT-6.2%20Sol) |
| DeepSeek 4.1 Flash vs GPT-6.2 Sol | 0:4 | [2 局](matches/06%20DeepSeek%204.1%20Flash%20vs%20GPT-6.2%20Sol) |
| Gemini Neon vs GPT-6.2 Sol | 0:4 | [2 局](matches/07%20Gemini%20Neon%20vs%20GPT-6.2%20Sol) |
| Gemini Neon vs DeepSeek 4.1 Flash | 4:1 | [3 局](matches/08%20Gemini%20Neon%20vs%20DeepSeek%204.1%20Flash) |
| Gemini Neon vs Qwen 4 | 4:0 | [2 局](matches/09%20Gemini%20Neon%20vs%20Qwen%204) |
| DeepSeek 4.1 Flash vs Qwen 4 | 1:4 | [3 局](matches/10%20DeepSeek%204.1%20Flash%20vs%20Qwen%204) |

## 参赛程序

每个文件夹包含比赛用的 `entrant.json` 和 `bot.py`。在裁判里选择对应文件夹即可加载，开发接口见 [SDK](../SDK/README.md)。

- [GPT-6.2 Sol](entrants/GPT-6.2%20Sol)
- [Gemini Neon](entrants/Gemini%20Neon)
- [Qwen 4](entrants/Qwen%204)
- [DeepSeek 4.1 Flash](entrants/DeepSeek%204.1%20Flash)
- [豆包 2.1 Pro](entrants/%E8%B1%86%E5%8C%85%202.1%20Pro)
