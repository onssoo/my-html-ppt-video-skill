# 上游血缘

本仓库基于 **[juguang/html-ppt-video-skill](https://github.com/juguang/html-ppt-video-skill)**（`master` 分支，2026-10-06 抓取）改造。

- **上游原版**保留在 `upstream/`（`SKILL.md` 212 行 + `build_video.py` 476 行 + README）—— **供对照，不参与运行**
- **工作版**：根目录的 `SKILL.md`（改写版说明书）+ `ppt2video.py`（替代上游 `build_video.py`）
- **上游 LICENSE**：见 `upstream/LICENSE`（如上游未附许可证，改造部分版权归 dailei）

## 为什么另开库

上游面向 **edge-tts（在线微软 TTS）+ Google Chrome + render.sh + brew ffmpeg**：
1. **edge-tts 无法克隆本人音色** —— 换 **mlx-audio Qwen3-TTS Base（克隆模式）**，edge-tts 保留为备选
2. **本机无 Chrome** —— 改用 **Microsoft Edge**（Chromium 内核，`--headless=new` 参数通用）
3. **本机无 brew / 无系统 ffmpeg** —— 改用 **imageio-ffmpeg 自带静态二进制**（实测**带 libass**，可烧字幕）
4. **上游逐页 mp4 片段 + concat** —— 上游自己的文档就写了 `-shortest` 会漂移；改为**一次音轨 + 一次视频**
5. **字幕按页估算** —— 改为**按句合成，起止 = 真实音频时长**
6. **新增**：句子级 TTS 缓存 · `-t` 硬截断（修 ffconcat 末帧播两遍）· `--source` 用**原始文档**写解说词 · `--pages` 只重写指定页

## 关联

- **母 skill（做 deck）**：`agent-skills/my-html-ppt-skill`（基于 lewislulu/html-ppt-skill）
- **本仓库的早期版本**：`agent-skills/html-ppt-video`（本地化 skill 的首版快照，已被本仓库取代）

## 实测依据

所有改动都有实测数字记录在 `SKILL.md` 的「Critical Pitfalls」里（字号、时长、相似度、坑的复现方法）。
