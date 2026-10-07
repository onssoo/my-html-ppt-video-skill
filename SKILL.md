---
name: html-ppt-video
description: Use when converting a document or article into a narrated HTML presentation video with synchronized Chinese TTS audio and subtitles. Triggers when user mentions "PPT转视频", "文档转视频", "生成讲解视频", "配音幻灯片", "把PPT做成带配音的视频", "文档做成视频", or wants to turn any written content into a narrated slide video with voiceover. Also use when user has an existing HTML PPT and wants to add narration and export as video.
---

# HTML PPT Video（本地化版 · M2）

把文档 → HTML PPT → 带中文配音和字幕的讲解视频。

> **本文件是 `juguang/html-ppt-video-skill` 的本地化改造版。**
> 上游用 edge-tts + Google Chrome + render.sh + brew ffmpeg；本机（M2）都没有或不适用，
> 已按实测结果替换。**改动清单和实测依据见文末「与上游的差异」。**

## Dependencies（M2 实测已就位）

| 依赖 | 本机实际情况 | 检查方式 |
|------|------|------|
| **mlx-audio + Qwen3-TTS** | `~/.venv-mlx-audio`；模型 `Qwen3-TTS-12Hz-1.7B-Base-8bit`（克隆） | `ppt2video.py check` |
| **ffmpeg** | ⚠️ **系统没有**；用 `imageio-ffmpeg` 自带的静态二进制 7.1，**自带 libass**（能烧字幕） | `resolve_ffmpeg()` 自动找 |
| **浏览器** | ⚠️ **没有 Chrome**；用 **Microsoft Edge**（Chromium 内核，`--headless=new --screenshot` 参数通用） | `resolve_browser()` 自动找 4 个路径 |
| **中文字幕字体** | 用 macOS 自带 **PingFang SC**（不需要 Noto Sans SC） | 烧字幕时 `force_style` 指定 |
| **html-ppt skill** | Phase 1 做 PPT 用；本机 `~/html-ppt-skill`（`lewislulu/html-ppt-skill`） | — |
| **cn2an** | 书面数字 → 口语读法（`40%`→"百分之四十"）；`~/.venv-mlx-audio` 已装 | `import cn2an` |
| ~~edge-tts~~ | 备用通道，未装（要装：`uv pip install edge-tts`） | — |
| ~~render.sh~~ | **不再使用** —— 直接调浏览器截图 | — |

## Workflow

### Phase 1: 文档 → HTML PPT

调用 **html-ppt skill** 制作。要点：

1. **规划结构**：封面 → 路线图 → 内容页（每页一个核心观点）→ 要点回顾 → 结尾
   - 目标 **10-16 页**（对应 3-5 分钟视频）
2. **选模板**：`~/html-ppt-skill/templates/full-decks/` 下有 15 套完整模板
   - 技术分享 → `tech-sharing`；商务汇报 → `corporate-clean`（主题）
   - 知识架构 → `knowledge-arch-blueprint`；课程 → `course-module`
3. **新建 deck**：`~/html-ppt-skill/scripts/new-deck.sh my-talk`
4. **页数不用手工确认** —— 脚本自动识别 `<section class="slide">` 的数量

**⚠️ 结构要求**（已用真实模板验证）：
```html
<section class="slide" data-title="Objectives">      ← 类名必须是 slide
<section class="slide full" data-title="Cover">      ← full 等附加类无妨
```
运行时按 **`#/N`（1-based）** 深链翻页 —— 与脚本默认 `--hash-start 1` 一致。

**解说词相关属性**：
- `data-sec="50"`：本页解说词的目标秒数（用于字数下限与"偏短"告警）。
- `data-disclaimer`：本页是财务预测 / 结论页，解说词**必须**带免责语；其他页**不得**出现。
- `<div class="notes">`：本页的讲述要点（解说词的依据，也参与数字溯源）。

### Phase 1.5: PPT 视觉确认（不可跳过）

```bash
open <deck-path>/index.html
```
让用户确认风格、主题（按 `T` 实时切换）、排版、页数。**确认后再进 Phase 2** ——
避免在不满意的 PPT 上白跑配音。

### Phase 2: 截图 + 生成确认文档

```bash
PY=~/.venv-mlx-audio/bin/python
$PY ~/ppt2video.py review deck/index.html
```
产出 `deck/video-output/`：
- `slides/001.png …`（Edge 无头截图，1920×1080；编号 **3 位**，>99 页不乱序）
- `outline.md`（提取出的每页文字）
- `review.md`（**核心确认文档**）

### Phase 2.5: 让 LLM 写解说词（逐页）

```bash
$PY ~/ppt2video.py narrate deck/index.html --source 原文.md --brief deck/brief.md
```
- **逐页生成**（不再整篇一次）—— 整篇模式会让模型套"承接→结论→引出"的固定结构，写出电报体碎句。
- 默认走 **DGX 上的 infersight 网关**（`http://100.89.119.47:9000/v1`，模型 `main`，temperature 0.5）。可用 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 覆盖。
- `--source`：原始文档，切成块后按相关性喂给每页，做**数字溯源**（解说词里的数字必须能在画面/要点/源文档里找到）。
- `--brief`：场景卡（brief.md），提供语气、主线、**口径表**（关键数字唯一写法）、术语表。
- 语气规范见 **`narration-spec.md`**（正式陈述、无自问自答、无套话、无俚语、无画面指代、免责语只在 `data-disclaimer` 页）。
- 每页过 **lint**（字数下限、问句、套话、碎句、禁用符号、免责语位置、数字溯源），不合格自动重写（最多 2 次）。
- 产出 `narrations.json`，并**自动同步 `review.md`**（保留原配置头），可直接 `build`，无需再跑 `review --reset`。

### Phase 3: 用户确认（改 review.md）

`review.md` 顶部是 **YAML 配置块，这才是配置的真源**（改脚本里的 `DEFAULTS` 会被它覆盖）：

```yaml
ref_audio: /Users/<user>/voice.wav     # 参考录音（克隆音色）
ref_text: 嗯，大家好，…                  # 必须与录音一字不差
instruct:                                # ⚠️ 克隆模式下无效，见「坑」
speed: 1.15                              # ✅ 基线（ffmpeg atempo 后处理）
subtitles: burn                          # burn 烧录 / soft 软字幕 / off
```

每页：`# N · 标题` + `- [ ]` checkbox + 图片 + ``` 代码块里的解说词 ```。
勾成 `- [x]` 跳过该页。

### Phase 4: 出片

```bash
$PY ~/ppt2video.py build deck/index.html
```
1. 一页的所有句子一次 `batch_generate`（共享参考条件 → 句间音色稳）
2. 每句独立音频 → 字幕起止时间 = **真实音频时长**（不是估算）
3. **TTS 读口语、字幕用书面**：解说词按书面写（`40%`、`120ms`），`speak()` 用 cn2an + 读音规则 + `pronounce.json` 转成读法再送 TTS；字幕显示的是书面原文
4. 按 `speed` 做 atempo 变速（**只重跑 ffmpeg，缓存复用，约 2 秒**）
5. **只合成一次音轨、一次视频**（不逐页切片再拼）→ 结构上无累积漂移
6. `-t <总时长>` 硬截断 → 视频与音轨严格等长

## 解说词规范（narration-spec.md）

正式口头陈述，不是聊天。核心规则（完整见 `narration-spec.md`）：

- **语气**：严谨（判断有依据、数字口径一致）、正向（讲进展与机会）、自信（陈述句给判断）、适度激情（每页至多一句意义点）。
- **语言**：规范书面语、只用陈述句（无问句/自问自答）、无口语俚语、无标签式引导语、不提及画面/表格/页码/左右位置、不用"首先其次最后"罗列。
- **事实与确定性**：区分已实现/进行中/规划/预测，不把低一级说成高一级；"第一""领先"要有依据并保留口径。
- **竞争对手**：只陈述可核实事实，中性词描述差异，不评价对手优劣。
- **免责**：只出现在 `data-disclaimer` 页，措辞固定。
- **书写格式**：数字/百分比/单位用阿拉伯数字，由 `speak()` 转读法；字幕显示书面写法。
- **配套**：`brief.md`（场景卡：受众/语气/主线/口径表/术语表/风格锚点）+ 可选 `pronounce.json`（专有名词读音）。

## Critical Pitfalls（全部实测踩过）

### ① 模型路径必须是本地绝对路径
用 HuggingFace 仓库 ID（如 `mlx-community/Qwen3-TTS-...`）会触发联网下载，
而**本机 HF 直连被墙**（`SSL: UNEXPECTED_EOF`）。`load_model` 只认本地目录。
**另**：mlx-audio 要求目录里有 **`config.json`**；魔搭上的**官方 PyTorch 版只有 `config.yaml`**，
直接下会加载失败（`Config not found`）—— **必须下 `mlx-community/*` 的 MLX 转换版**。

### ② 克隆模式不支持 instruct（库明确拒绝）
```
ValueError: Qwen3-TTS batch reference cloning does not support instructs
```
→ **语气只能靠"换参考录音"**：参考里什么情绪，输出就是什么情绪。

### ③ 模型自带的 speed 参数在克隆模式无效
实测同一句话：`speed=1.0→4.72s`、`1.15→4.96s`、`0.85→4.64s` ——
差异（±5%）小于采样噪声。**变速只能靠 ffmpeg `atempo`**（保音高）。

### ④ 不设 repetition_penalty 会翻车
实测不设（=1.0）时，44 字念出 **32.00 秒**（一路念满 `max_tokens`）。
官方 `generation_config.json` 要求 **1.05**。脚本在 4 次重试里**逐次加大**（1.05→1.29）。

### ⑤ review.md 覆盖脚本 DEFAULTS
配置真源是 `review.md`。改了脚本默认值但没跑 `review --reset`，build 仍用旧配置。
**解说词同理**：build 以 `review.md` 里的解说词为准，`narrate` 已自动同步它；
若手工改了 `narrations.json` 却没同步 `review.md`，build 会用旧词。

### ⑥ ffconcat 最后一帧导致最后一页播两遍
`list.ffconcat` 末行重复的 `file` 没有 `duration`，ffmpeg 补一次默认时长
（实测日志 24.67s → 视频 30.57s）。**修法：mux 加 `-t <总时长>` 硬截断。**

### ⑦ 短句音色偏弱，但不要合并
实测"大家好。"（4 字，0.8s）与其他句的声纹相似度只有 0.91~0.94，长句是 0.96~0.99。
**试过并入下一句（音色升到 0.99），但用户判断更难听 —— 停顿被吃掉了。**
→ **保留句边界和停顿**，接受短句略弱。

### ⑧ `batch_generate` 的参数是复数
`instructs=[...]`（列表），不接收单数 `instruct`；`texts=[...]`。

### ⑨ 书面数字直接送 TTS 会读错
`40%`、`120ms`、`2030` 直接念会被读成"四零百分之""一二零毫秒"。
**修法**：`speak()` 先过 cn2an（`40%`→"百分之四十"）+ 单位规则 + `pronounce.json` 专有名词，再送 TTS。
单位规则用 `(?![A-Za-z])` 前瞻而非 `\b`（Python `re` 把 CJK 当 `\w`，`\b` 在中文前失效，`120ms降` 里的 `ms` 转不了）。

### ⑩ 字数是下限不是上限（内容优先）
正式解说词信息量足，**15+ 分钟是常态**。`lint` 只查"太短"（内容被砍），不查"太长"；
build 只在 `dur/sec < 0.8`（明显偏短）时告警。不要为凑时长删内容。

## Output Structure

```
deck/video-output/
├── slides/001.png …       # Edge 无头截图 1920×1080
├── outline.md             # 提取的每页文字
├── review.md              # ★ 配置真源 + 解说词 + 逐页确认
├── narrations.json        # narrate 产出（书面解说词）
├── brief.md               # 场景卡（narrate --brief 写入；口径表/术语表/风格锚点）
├── source.md              # 原始文档（narrate --source 写入，便于核对）
├── pronounce.json         # 可选：专有名词/化学元素读音覆盖
├── tts-cache/             # 句子级缓存（含 *_x1.15.wav 变速产物）
├── narration.wav          # 合成后的完整音轨
├── list.ffconcat          # 视频帧序列
├── subs.srt               # 字幕（句级真实时长，书面写法）
└── final-video.mp4        # ★ 成片
```

## Tuning（三项可调，均已实测）

| 想调 | 怎么调 | 成本 |
|---|---|---|
| **速度** | 改 `review.md` 的 `speed:`（1.0~1.2 自然，>1.25 听得出）| ✅ **约 2 秒重出片**（缓存复用，只跑 ffmpeg）|
| **情感/语气** | **重录一条那个情绪的参考录音** + 改 `ref_audio`/`ref_text` | ⚠️ 要重新合成 |
| **音色** | 同上 | ⚠️ 同上 |

参考录音要求：**3~10 秒、内容连贯（不要几段不相干的话）、包含会用到的词（尤其数字）、
无"嗯"等口头语、语速平稳、安静环境一条到底**。
（实测：参考文本里含"大家好"时，合成"大家好"相似度 0.9647；不含时只有 0.9318。）

## 与上游的差异

| # | 上游 | 本地版 | 依据 |
|---|---|---|---|
| 1 | `render.sh` 截图 | 直接调浏览器（Edge/Chrome 自动探测）| M2 无 Chrome；render.sh 是 html-ppt skill 的可选脚本 |
| 2 | 手工确认页数 | 自动识别 `<section class="slide">` | 减少一步人工 |
| 3 | edge-tts（在线）| **mlx-audio Qwen3-TTS 克隆**（edge-tts 保留为备选）| 要克隆本人音色；且不依赖在线服务 |
| 4 | 按页估算字幕时间 | **按句合成，起止=真实音频时长** | 字幕精度 |
| 5 | 逐页 mp4 片段 + concat | **一次音轨 + 一次视频** | 上游"Critical Pitfalls"自己写的 `-shortest` 漂移问题，从结构上根除 |
| 6 | 无缓存 | **句子级 TTS 缓存** | 改一页只重合成改动那几句 |
| 7 | 解说词靠 agent 手写 | **`narrate` 直连 DGX infersight，逐页生成 + lint 自动重写** | 不需要 agent 框架；杜绝电报体碎句 |
| 8 | brew ffmpeg + ffprobe | `imageio-ffmpeg` 静态二进制（自带 libass）| M2 没 brew；且不需要 ffprobe（时长用 Python 的 wave 读）|
| 9 | Noto Sans SC | PingFang SC（macOS 自带）| 不额外装字体 |
| 10 | 4 个 edge-tts 音色 | **克隆本人音色** + 上述三项调参 | — |
| 11 | 无 `-t` 兜底 | mux 加 `-t <总时长>` | 修 ffconcat 末帧播两遍 |
| 12 | 无时长合理性检查 | 按字数判定（0.16~0.50 s/字），异常重试并逐次加大重复惩罚 | 防翻车 |
| 13 | 无书面/口语分离 | **`speak()`：cn2an + 单位规则 + `pronounce.json`，TTS 读口语、字幕留书面** | 数字/单位读法正确，字幕可读 |
| 14 | 无语气规范 | **`narration-spec.md` + `brief.md` 口径表 + 数字溯源** | 正式陈述、口径一致、防幻觉数字 |

## Troubleshooting

| 现象 | 原因 | 解决 |
|---|---|---|
| `Config not found` | 下的是官方 PyTorch 版（只有 config.yaml）| 换 `mlx-community/*` 的 MLX 版 |
| HF 连接超时 | 用了 HF 仓库 ID | 改成本地绝对路径 |
| `does not support instructs` | 克隆模式不支持 instruct | 去掉 instruct，改用参考录音控制语气 |
| 某句音色突然不像 | 该句过短（<1 秒）| 正常现象；可把该句写长一点 |
| 视频比日志长一页 | ffconcat 末帧缺 duration | 已有 `-t` 兜底；若仍出现检查 `total` 计算 |
| 截图全是同一页 | deck 不支持 `#/N` 翻页 | reveal.js 用 `--hash-start 0` |
| 改了配置没生效 | `review.md` 覆盖了 DEFAULTS | 直接改 `review.md`，或 `review --reset` |
| 字幕是方块 | 字体缺失 | 本版用 PingFang SC；换机器需确认系统有中文字体 |
| 数字读法不对（40%→四零）| 没过 `speak()` | 确认 build 走了 `speak()`；单位规则用 `(?![A-Za-z])` 前瞻 |
| 专有名词读错（Pt/Au）| 缺读音覆盖 | 在 `video-output/pronounce.json` 加 `{"Pt":"铂"}` |
| 解说词出现套话/问句 | LLM 没遵守规范 | `narrate` 的 lint 会自动重写；仍出现则调 `narration-spec.md` 措辞 |
| 数字对不上画面 | 模型幻觉 | 确认 `--source` 传了原文；数字溯源会退回重写 |
