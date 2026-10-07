# 上游血缘

本仓库基于 **[juguang/html-ppt-video-skill](https://github.com/juguang/html-ppt-video-skill)**（`master` 分支，2026-10-06 抓取）改造。

- **上游原版**保留在 `upstream/`（`SKILL.md` 212 行 + `build_video.py` 476 行 + README）—— **供对照，不参与运行**
- **工作版**：根目录的 `SKILL.md`（改写版说明书）+ `ppt2video.py`（替代上游 `build_video.py`）
- **上游 LICENSE**：`LICENSE`（本仓库根目录，原样保留，**不得删除**）

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

所有改动都有实测数字记录在 `SKILL.md` 的「`references/pitfalls.md`」里（字号、时长、相似度、坑的复现方法）。

---

## 与上游的实现差异（本地化改造）

| # | 上游 | 本地版 | 依据 |
|---|---|---|---|
| 1 | `render.sh` 截图 | 直接调浏览器（Edge/Chrome 自动探测）| M2 无 Chrome；render.sh 是 html-ppt skill 的可选脚本 |
| 2 | 手工确认页数 | 自动识别 `<section class="slide">` | 减少一步人工 |
| 3 | edge-tts（在线）| **mlx-audio Qwen3-TTS 克隆**（edge-tts 保留为备选）| 要克隆本人音色；且不依赖在线服务 |
| 4 | 按页估算字幕时间 | **按句合成，起止=真实音频时长** | 字幕精度 |
| 5 | 逐页 mp4 片段 + concat | **一次音轨 + 一次视频** | 上游"`references/pitfalls.md`"自己写的 `-shortest` 漂移问题，从结构上根除 |
| 6 | 无缓存 | **句子级 TTS 缓存** | 改一页只重合成改动那几句 |
| 7 | 解说词靠 agent 手写 | **`narrate` 直连 DGX infersight，逐页生成 + lint 自动重写** | 不需要 agent 框架；杜绝电报体碎句 |
| 8 | brew ffmpeg + ffprobe | `imageio-ffmpeg` 静态二进制（自带 libass）| M2 没 brew；且不需要 ffprobe（时长用 Python 的 wave 读）|
| 9 | Noto Sans SC | Noto Sans SC（macOS 自带）| 不额外装字体 |
| 10 | 4 个 edge-tts 音色 | **克隆本人音色** + 上述三项调参 | — |
| 11 | 无 `-t` 兜底 | mux 加 `-t <总时长>` | 修 ffconcat 末帧播两遍 |
| 12 | 无时长合理性检查 | 按字数判定（0.16~0.50 s/字），异常重试并逐次加大重复惩罚 | 防翻车 |
| 13 | 无书面/口语分离 | **`speak()`：cn2an + 单位规则 + `pronounce.json`，TTS 读口语、字幕留书面** | 数字/单位读法正确，字幕可读 |
| 14 | 无语气规范 | **`references/narration-spec.md` + `brief.md` 口径表 + 数字溯源** | 正式陈述、口径一致、防幻觉数字 |
| 15 | 只吃 HTML PPT | **`import` 直接吃 PPTX / PDF**（PyMuPDF 渲染 + python-pptx 提取 + LibreOffice 兜底）| 已有幻灯片不必重做；画面定稿时省掉整个 Phase 1 |
| 16 | 时长固定 | **多版本（`versions.json` + `--version`）**：总时长按权重换算每页字数；短版从母版压缩；`cps.json` 实测校准 | 同一份 PPT 对不同对象讲不同时长，画面只截一次 |
| 17 | 年份读法错、大额金额单位别扭、TTS 无重试 | **年份逐位读法（19xx/20xx/21xx）+ 万元 ≥1 亿折算成亿元 + 合成 3 次退避重试 + cps 指纹含读法规则** | `2026 年` 不再念成"二千零二十六年"、`100000 万元` 不再念成"十万万元"；瞬时抖动不再打断整片 |
