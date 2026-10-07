---
name: html-ppt-video
description: Use when converting a document, article, or existing slide deck into a narrated presentation video with synchronized Chinese TTS audio and subtitles. Triggers when user mentions "PPT转视频", "文档转视频", "生成讲解视频", "配音幻灯片", "把PPT做成带配音的视频", "文档做成视频", "给PPT配音", "PDF做成视频", or wants to turn any written content or existing PPTX/PDF slides into a narrated slide video with voiceover. Also use when user has an existing HTML PPT and wants to add narration and export as video, or has existing PPTX/PDF slides (use `import` to skip the slide-authoring step).
---

# HTML PPT Video（本地化版 · M2）

把**文档 / 已有幻灯片**做成带中文配音和字幕的讲解视频。

两条路线，从「写解说词」往后完全一样：

| 路线 | 输入 | 画面从哪来 | 用到的命令 |
|---|---|---|---|
| **A** | 文档、文章 | 先做成 HTML PPT，再逐页截图 | `review` → `narrate` → `build` |
| **B** | **已有的 PPTX / PDF** | `import` 直接把幻灯片渲染成图片 | `import` → `narrate` → `build` |

路线 B 更短：画面已经定稿，不用生成幻灯片，也不用跑几何检查。

> **本文件是 `juguang/html-ppt-video-skill` 的本地化改造版。**
> 上游用 edge-tts + Google Chrome + render.sh + brew ffmpeg；本机（M2）都没有或不适用，
> 已按实测结果替换。**改动清单和实测依据见文末「与上游的差异」。**

## Dependencies（M2 实测已就位）

| 依赖 | 本机实际情况 | 检查方式 |
|------|------|------|
| **mlx-audio + Qwen3-TTS** | `~/.venv-mlx-audio`；模型 `Qwen3-TTS-12Hz-1.7B-Base-8bit`（克隆） | `ppt2video.py check` |
| **ffmpeg** | ⚠️ **系统没有**；用 `imageio-ffmpeg` 自带的静态二进制 7.1，**自带 libass**（能烧字幕） | `resolve_ffmpeg()` 自动找 |
| **浏览器** | ⚠️ **没有 Chrome**；用 **Microsoft Edge**（Chromium 内核，`--headless=new --screenshot` 参数通用） | `resolve_browser()` 自动找 4 个路径 |
| **中文字幕字体** | `mux()` 里写死 `FontName=Noto Sans SC`；本机若没这个字体会由 fontconfig 回退到系统中文字体（实测能正常显示，不变方块） | 改 `mux()` 里的 `force_style` |
| **html-ppt skill** | Phase 1 做 PPT 用；本机 `~/html-ppt-skill`（`lewislulu/html-ppt-skill`） | — |
| **cn2an** | 书面数字 → 口语读法（`40%`→"百分之四十"）；`~/.venv-mlx-audio` 已装 | `import cn2an` |
| **pymupdf**（路线 B） | PDF 逐页渲染 + 提取文字；`pip install pymupdf` | `ppt2video.py check` |
| **python-pptx**（路线 B） | 提取标题/文字/表格/图表数据/演讲者备注；`pip install python-pptx` | `ppt2video.py check` |
| **LibreOffice**（路线 B，可选） | PPTX 没有同名 PDF 时才用；`/Applications/LibreOffice.app` | `SOFFICE` 环境变量可覆盖 |
| ~~edge-tts~~ | 备用通道，未装（要装：`uv pip install edge-tts`） | — |
| ~~render.sh~~ | **不再使用** —— 直接调浏览器截图 | — |

## Workflow

先选路线（见上表）。**路线 B 跳过下面的 Phase 1，直接从 Phase 2.5 开始。**

### 路线 B: 已有 PPTX / PDF → import

```bash
PY=~/.venv-mlx-audio/bin/python

# 1. 导入：渲染截图 + 生成 slides.json
$PY ~/ppt2video.py import old/融资路演.pptx

# 2. 检查 slides/ 下的截图；在 slides.json 里设置每页 sec / disc / notes
#    （财务页和结尾页设 "disc": true；没有备注的页补 notes）

# 3. 写解说词（图表多、文字少的话加 --vision，需多模态模型）
$PY ~/ppt2video.py narrate old/融资路演.pptx --source source/report.docx --brief brief.md

# 4. 出片（旧幻灯片建议在 review.md 开头设 frame: letterbox）
$PY ~/ppt2video.py build old/融资路演.pptx
```

- **PDF**：PyMuPDF 按 1920×1080 的画布逐页渲染成 PNG，同时提取每页文字。PDF 里没有演讲者备注。
- **PPTX**：python-pptx 提取标题、文字、表格、图表里的数据和**演讲者备注**（备注当"讲述要点"用；
  旧 PPT 的备注常常就是当年的讲稿，是很好的素材）。截图优先用**同名且更新的 PDF**；
  没有才调 LibreOffice 转换。
- **`slides.json` 可以手工编辑**：每页的 `sec`（目标秒数）、`disc`（是否需要免责说明）、
  `notes`（讲述要点）都能改。这三项对应 HTML 流程里的 `data-sec`、`data-disclaimer` 和 `notes`，
  解说词规范照样生效。**重新导入时会保留你改过的这三项。**
- 输出目录：HTML 用 `video-output/`，PPTX/PDF 用 `<文件名>-video/`，同一目录放多份幻灯片不会互相覆盖。
- 页数**不用手工确认**，`import` 会打印；PPTX 的隐藏页自动跳过。

#### 还原度：PPTX 最好自己先导出 PDF

用 PowerPoint 或 Keynote 导出 PDF，放在同一目录、**使用同名文件**，脚本会直接用。LibreOffice 转换有三个常见问题：

- **字体被替换**：Mac 上没有微软雅黑、等线这类字体时，LibreOffice 会换成别的字体，导致换行和排版变形。
- **部分元素显示异常**：SmartArt、某些图表样式和特效可能显示不正确。
- **分步动画全部叠在一起**：PDF 只有静态画面，原本分步出现、或者出现后又消失的元素，会同时显示在同一页上。

所以导入后**一定要打开 `slides/` 目录，逐页检查一遍截图**。

#### 几点说明

- **旧稿里的数字要先进口径表**：旧 PPT 里的数字可能和现在的源文档不一致（做 PPT 时还是老版本的预测）。
  画面已经无法修改，解说词又必须和画面一致，所以先核对，把两边的差异在 `brief.md` 的口径表里讲清楚。
- **备注已经是成稿的情况**：如果某份 PPT 的备注本身就是写好的讲稿，`narrate` 会把它当作讲述要点**重新改写**。
  想直接用原文需要另加 `--from-notes`（**尚未实现**，需要时提）。
- **PDF 必须是幻灯片版式**：A4 竖版的报告类 PDF 也能导入，但画面会是左右留大片空白的竖版页面，不适合做视频。

### 路线 A · Phase 1: 文档 → HTML PPT

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

## 多版本：同一份幻灯片，不同时长

同一份 PPT 对不同对象要讲不同时长（10 分钟给投资人、15 分钟给深度沟通、8 分钟给合作方）。
时长做成版本，**画面只截一次，解说词/审稿/成片各版本独立**。

### 四个控制参数

| 参数 | 写在哪 | 作用 |
|---|---|---|
| **总时长** | `versions.json` 的 `minutes` | 该版本的目标分钟数 |
| **页权重** | `data-sec`（HTML）/ `slides.json` 的 `sec`（导入）| 相对权重，不是秒数；缺省 45。重点页给大值 |
| **固定页** | `data-fixed="12"` / `slides.json` 的 `fixed` | 封面、目录、免责页固定秒数，不参与缩放 |
| **跳页** | `versions.json` 的 `skip: [11, 12]` | 短版整页跳过附录/明细，比每页压到 15 秒好 |

**关键：不要把"讲 10 分钟"直接告诉模型。** 模型对时长没有概念，字数会偏得很远。脚本按下面的公式把
总时长换算成**每页字数目标**，模型只面对一个明确的字数。

```
每页秒数 → 字数：  speech = max(sec − LEAD − TAIL, 3)
                  chars  = speech × cps / (1 + GAP × cps / 25)
```
（`LEAD/TAIL/GAP` 是页首留白/页尾留白/句间停顿；`cps` 是实测语速，见下）
按 cps=4.5 估算：**30 秒 ≈ 125 字，45 秒 ≈ 190 字**。

- 每页有下限 `MIN_SEC=12` 秒。**钳制后总长会超标，脚本会报出来**：
  "因每页下限 12 秒，实际总长 X 分钟 > 目标 Y 分钟" → 这时该用 `skip` 砍页，而不是硬压每页。
- 短版的**上限是软的**：超过目标上限 20%（`OVER_TOL`）才退回重写。内容优先，不硬卡上限。

### versions.json（放在幻灯片旁边）

```json
{
  "investor-15": {"minutes": 15, "brief": "brief-investor.md"},
  "investor-10": {"minutes": 10, "brief": "brief-investor.md", "from": "investor-15"},
  "partner-8":   {"minutes": 8,  "brief": "brief-partner.md", "skip": [11, 12], "from": "investor-15"}
}
```

**先做最长的母版，短版从母版压缩**（`from`）。压缩不会冒出新的编造内容——母版文本也会进数字溯源。
反过来把短版扩写成长版容易编造，所以**长版必须单独生成**，不要从短版扩写。
受众差异很大时（投资人 vs 技术合作方）用不同 `brief` 重新生成，不走压缩。

母版更新后，脚本会按文件时间提醒"本版可能已过期"。

### 配置层级（四个文件，各管一件事）

| 文件 | 位置 | 管什么 |
|---|---|---|
| `versions.json` | 幻灯片旁边 | **版本级**：总时长、页权重来源、跳页、场景卡、母版来源 |
| `review.md` | `versions/<版本>/` | **该版本的真源**：音色、语速倍数、字幕、构图 + 逐页解说词 |
| `cps.json` | 输出根目录 | 实测语速（**与音色/后端/语速绑定**，各版本共用） |
| `slides.json` | 输出根目录 | 导入的画面文字 + 每页 `sec`/`fixed`/`disc`/`notes`（各版本共用） |

`brief` 优先级：命令行 `--brief` > `versions.json` 的 `brief`。

### 输出目录

```
<deck>-video/
├── slides/001.png …            # 画面只截一次，各版本共用
├── slides.json                 # 导入信息（各版本共用）
├── cps.json                    # 实测语速（各版本共用）
├── tts-cache/                  # 句子级缓存（各版本共用：相同句子只合成一次）
├── versions/<版本名>/           # 该版本的一切
│   ├── narrations.json  review.md  brief.md  outline.md
│   ├── narration.wav  list.ffconcat  subs.srt
│   └── final-<版本名>.mp4
└── final-video.mp4             # 只有不带 --version 的单版本模式才有
```

### 使用流程

```bash
# 1. 母版（最长版本）＋校准语速
python ppt2video.py narrate 路演.pptx --version investor-15 --source report.docx
python ppt2video.py review  路演.pptx --version investor-15 --reset
python ppt2video.py build   路演.pptx --version investor-15    # 写入 cps.json

# 2. 从母版压缩出短版（只需在 versions.json 里加一行）
python ppt2video.py narrate 路演.pptx --version investor-10
python ppt2video.py review  路演.pptx --version investor-10 --reset
python ppt2video.py build   路演.pptx --version investor-10
```

`--version` **不传就是老的单版本流程**（产物落在输出根目录，字数下限用 `--length`），
现有 deck 不受影响。

### 校准与容差

- `build` 结束会打印 **实测字/秒**，并写入 `cps.json`。之后的版本自动用它，时长会越来越准。
- `cps.json` 记录后端/音色/语速**指纹**：换了参考录音、TTS 后端或改了 `speed`，指纹不匹配 →
  自动作废并按默认 4.5 估算，同时提示重做母版校准。（`speed` 走 ffmpeg atempo，直接改字/秒。）
- 每页低于 20 秒基本只能念结论；20 页的 PPT 比较舒服的范围是 **8–20 分钟**。
- 还差几个百分点时不必重写：微调 `GAP`/`TAIL`（各 0.1 秒 × 20 页 ≈ 十几秒），
  或对个别页用 `--pages` 只重写那几页。
- **短版审稿重点看取舍**：压缩可能删掉你认为重要的内容。某页总被删错时，
  最根本的办法是把那条要点**移到讲述要点的第一条**，而不是反复重新生成。

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

**年份必须单独逐位读**（实测在 M2 上暴露）：`2026 年` 交给 cn2an 会念成"**二千零二十六年**"，
正确的口播是"二零二六年"。`_SPEAK_RULES` 里有两条年份规则，且**必须排在区间规则之前**
（否则 `2026–2030 年` 先被拆成 `2026到2030 年`，两条年份规则就都匹配不上了）。
只认 `19xx/20xx/21xx`，这样 `1200 年历史` 不会被误读成"一二零零年"。

**金额到了"亿"的量级要折算单位**：万元数值 ≥ 10000（即 1 亿）→ 折算成亿元，
`100000 万元` → `10亿元` → 读"十亿元"；`98800万元` → `九点八八亿元`。
千万级及以下**保持"万元"**，由 cn2an 读成"七千三百万元"——那本来就是中文的自然说法。
区间也认：`7300–100000 万元` → `七千三百万元到十亿元`。
金额规则同样**必须排在通用区间规则之前**。

### ⑩ 字数是下限不是上限（内容优先）
正式解说词信息量足，**15+ 分钟是常态**。`lint` 只查"太短"（内容被砍），不查"太长"；
build 只在 `dur/sec < 0.8`（明显偏短）时告警。不要为凑时长删内容。

### ⑪ 导入的 PPTX 页数必须和 PDF 对得上
`import` 用 python-pptx 数可见页、用 PDF 数实际页，两者不一致就直接退出，不猜。
【实测】**LibreOffice 会跳过 PPTX 的隐藏页**（6 页含 1 页隐藏 → PDF 5 页，与可见页一致，能正常导入）。
报错时优先用 PowerPoint 自己导出 PDF，或用 `SOFFICE` 指定别的转换器。

### ⑫ 旧幻灯片里的数字可能和现在的源文档不一致
画面已定稿、无法再改，而解说词必须和画面一致。先把两边差异写进 `brief.md` 的口径表再生成解说词，
否则数字溯源会把"画面上的旧数字"判成无出处。

### ⑬ 图表数字的格式会影响数字溯源
`import` 从图表取值时把整数写成整数（`7300` 而不是 `7300.0`）。若出现"数字无出处"误报，
先看 `slides.json` 里那页的数字是怎么写的。

### ⑭ `--vision` 的数字脚本核验不了
看图模式下模型可能从图片里读出数字，脚本无法验证 → 只打印提醒、不退回重写，**审稿时重点核对**。
`--vision` 需要多模态模型。**实测边界**：文本模式的 `narrate` 已在真网关（`main`）跑通；
看图模式只验证了"请求体确实带上图片且与源文件字节一致"（本地假网关抓包），**没有**在真网关的多模态模型上跑过。

### ⑮ `data-sec` 在两种模式下含义不同
**单版本**：`data-sec="50"` = 这一页目标 50 秒（`page_floor` 按它推字数下限）。
**版本模式**：`data-sec` = **相对权重**（缺省 45），实际秒数由版本总时长分配。
同一属性两种含义是刻意保留的（老 deck 不受影响），改稿时别混。

### ⑯ `cps.json` 与后端/音色/语速/读法规则绑定
指纹含这四项。**改了 `speed` 也必须重新校准**——atempo 直接改字/秒，这一条最容易被忽略。
**读法规则变了同样要重新校准**：实测只是把年份改成逐位读法，同一句话的音频就变短了，
实测 cps 从 **4.43 跳到 5.16**；旧值若被沿用，之后所有版本的时长都会偏短。
指纹不匹配时脚本自动作废并提示，不会静默使用旧值。

### ⑰ `MIN_SEC` 会让总时长超标
每页下限 12 秒。20 页的目标若短于 4 分钟，钳制后总长必然超过目标——脚本会报出来。
正确做法是用 `skip` 砍页，而不是把每页压得更短。

## Output Structure

路线 A 输出到 `deck/video-output/`；路线 B 输出到 `<文件名>-video/`（同一目录放多份幻灯片不会互相覆盖）。

```
<deck>-video/
├── slides/001.png …       # 路线 A：Edge 无头截图；路线 B：PPTX/PDF 逐页渲染（都是 3 位编号）
├── slides.json            # 路线 B：每页 title / sec / fixed / disc / text / notes（可手工编辑）
├── <文件名>.pdf            # 路线 B：LibreOffice 转换产物（用同名 PDF 时不产生）
├── cps.json               # 实测语速（绑定后端/音色/语速；多版本共用）
├── versions/<版本名>/       # 用了 --version 时，该版本的一切都在这里（见「多版本」一节）
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

## Tuning（四项可调，均已实测）

| 想调 | 怎么调 | 成本 |
|---|---|---|
| **速度** | 改 `review.md` 的 `speed:`（1.0~1.2 自然，>1.25 听得出）| ✅ **约 2 秒重出片**（缓存复用，只跑 ffmpeg）|
| **情感/语气** | **重录一条那个情绪的参考录音** + 改 `ref_audio`/`ref_text` | ⚠️ 要重新合成 |
| **音色** | 同上 | ⚠️ 同上 |
| **字幕位置** | 改 `review.md` 的 `frame:`（`fit` 铺满 / `letterbox` 画面上移、底部留 120px 给字幕）和 `pad_color:` | ✅ **约 2 秒重出片** |
| **总时长** | `versions.json` 加一行（`minutes` + `from` + `skip`），再跑 narrate / review / build | ⚠️ 要重新生成解说词并配音；与前版相同的句子命中缓存 |

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
| 15 | 只吃 HTML PPT | **`import` 直接吃 PPTX / PDF**（PyMuPDF 渲染 + python-pptx 提取 + LibreOffice 兜底）| 已有幻灯片不必重做；画面定稿时省掉整个 Phase 1 |
| 16 | 时长固定 | **多版本（`versions.json` + `--version`）**：总时长按权重换算每页字数；短版从母版压缩；`cps.json` 实测校准 | 同一份 PPT 对不同对象讲不同时长，画面只截一次 |
| 17 | 年份读法错、大额金额单位别扭、TTS 无重试 | **年份逐位读法（19xx/20xx/21xx）+ 万元 ≥1 亿折算成亿元 + 合成 3 次退避重试 + cps 指纹含读法规则** | `2026 年` 不再念成"二千零二十六年"、`100000 万元` 不再念成"十万万元"；瞬时抖动不再打断整片 |

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
| 导入报"页数与 PDF 对不上" | PPTX 有隐藏页，或同名 PDF 是旧版本 | 用 PowerPoint 自己导出 PDF；或用 `SOFFICE` 指定别的转换器 |
| 截图里字体变了 / 元素错位 | LibreOffice 转换的字体替换与特效丢失 | 自己导出同名 PDF 放在 PPTX 旁边 |
| 分步动画全挤在一页 | PDF 是静态画面，分步出现或消失的元素会叠加 | 导出 PDF 前先把分步内容拆成多页 |
| 字幕压住画面底部内容 | 旧幻灯片没预留字幕区 | `review.md` 里设 `frame: letterbox` |
| `import` 报缺 pymupdf / python-pptx | 没装导入依赖 | `pip install pymupdf python-pptx` |
| 竖版 A4 报告导入后左右大片黑边 | PDF 不是幻灯片版式 | 报告类内容走路线 A 做 HTML PPT |
| 实际时长和目标差很多 | 语速估算不准，或被 `MIN_SEC` 钳制 | 看 build 打印的"实测字/秒"；先出一次母版校准 `cps.json` |
| 所有版本的时长都不对 | 改了 `speed` 但没重新校准 | 删掉 `cps.json`，重跑一次母版 |
| 短版把重要内容删了 | 压缩按讲述要点顺序取舍 | 把那条要点移到该页讲述要点的**第一条** |
| 某页解说词总因过长被退回重写 | 该页分到的秒数太少 | 调高该页 `sec` 权重，或把它从短版 `skip` 掉 |
| 母版改了，短版还是旧的 | 短版是母版压缩来的 | narrate 时会按时间提醒；重跑短版的 narrate |
| 年份念成"二千零二十六年" | 读法规则缺年份条目 | 已内置（`_SPEAK_RULES` 前两条）；若仍出现，检查是否被 `pronounce.json` 覆盖 |
| build 跑到一半报 TTS 错误 | 网络/推理瞬时抖动（edge 的 `NoAudioReceived`、网关 reset）| 已内置 3 次退避重试；仍失败就重跑，**已合成的句子命中缓存不会重做** |
| `100000 万元` 念成"十万万元" | 已内置折算：万元 ≥ 10000 自动读成亿元 | 若仍出现，检查数值是否 < 10000 万元，或被 `pronounce.json` 覆盖 |
