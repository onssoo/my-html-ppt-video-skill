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
| **浏览器/截图** | 截图走 **Playwright Chromium**（`render()`；找不到才回退 msedge）；`check_deck.py` 用同一引擎，保证检查通过的画面 = 截图画面 | `pip install playwright && playwright install chromium` |
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

### 路线 A：文档 → 大纲 → slides → 解说词 → 成片

**Phase 1 · 大纲（→ Gate ①）**

1. 按 **`business-deck-spec.md` §一** 定场景：受众 / 用途 / 时长 / 主线，同时写 `brief.md`
2. 写 `大纲.md`，格式按 `references/outline-schema.md`：
   `## P1 结论式标题` + `- 版式：` + `- 信息点（N）：` + `- 时长：45 秒` + `- 讲述要点（3–5 条，每条注明出处）`
3. `python ppt2video.py outline 大纲.md` → `大纲-审核报告.md`（按规范 §一/§二 预检 + 人工审核要点）。**ERROR 必须清零**
4. **停下来 → Gate ①**：owner 在审核台逐页改，或直接改 `大纲.md`

**Phase 2 · 做 slides**

1. `python ppt2video.py deck-skeleton 大纲.md` → `deck-骨架.html`
   （每页一个 `<section class="slide" data-title="…" data-sec="45">` + 隐藏的 `<div class="notes">`）
2. 版式从 `html-ppt-skill/references/layouts.md` 的骨架里选；模板 `dailei-business-deck`、主题 `dailei-business-navy`
3. `check_deck.py deck/index.html --footer ".bd-foot" --fonts "Noto Serif SC,Noto Sans SC,Inter"`
   （规范 §七：逐页几何 / 字号 / 填充率 / 外链 / 字体加载）—— **ERROR 清零**，再人工看一遍

**Phase 3 · 解说词（→ Gate ②）**

```bash
$PY $P2V review deck/index.html                       # 截图 + 生成 review.md
$PY $P2V narrate deck/index.html --source 原文.md --brief brief.md
$PY $P2V deck-pdf   deck/index.html                   # 画面预览 PDF
$PY $P2V review-doc deck/index.html                   # 解说词审定稿
$PY $P2V review-ui  deck/index.html                   # 审核台（本机起，点"完成"退出）
```
- `review.md` 顶部的 YAML 是**配置真源**（`ref_audio` / `speed` / `subtitles`…）；每页 `# N · 标题` + checkbox + 图片 + 代码块里的解说词，勾 `[x]` 跳过该页
- ⚠ **deck 项目若纳入 git**：`review.md` 里有参考录音路径与逐字文本，`video-output/` 要加进 `.gitignore`
  （本仓库的 `.gitignore` 已忽略 `video-output/` 与 `*-video/`，deck 项目要自己加）
- `data-sec="45"` = 本页解说词的**目标秒数**（绝对秒，不是权重）；`<div class="notes">` = 讲述要点，也是数字溯源依据
- **免责默认关闭**：deck 不做免责页，解说词也不写免责/声明/风险提示；只有 owner 明确要求才用 `--disclaimer`
- **停下来 → Gate ②**：owner 看画面 + 解说词

**Phase 4 · 出片（→ Gate ③）**

```bash
$PY $P2V build deck/index.html        # 一页句子一次 batch；字幕起止 = 真实音频时长
$PY $P2V qc    deck/index.html        # 质检报告：时长偏差 / 逐页对比 / 音量 / 字幕抽样 / 重录表
```
- 出片前会查 `gates.json`：deck / 解说词 / review.md 在通过 Gate ② 之后被改过 → **拒绝出片**
- **停下来 → Gate ③**：owner 听音色与读音、看字幕遮挡；读错字加进 `pronounce.json` 再重跑

### 路线 B: 已有 PPTX / PDF → import

命令是 `ppt2video.py import <文件>`，细节见 **`references/route-b-import.md`**
（逐页看截图、在 `slides.json` 里改 `sec`/`fixed`/`notes`、旧稿数字口径要先对齐）。

## 三个审核点（Gate 1 / 2 / 3）

每个阶段收尾都要产出**给人审的工作文件**，owner 改完再进下一步。三条命令都是**确定性的**（不调 LLM），随时可重跑。

| Gate | 什么时候 | 命令 | 产物 | owner 能改什么 |
|---|---|---|---|---|
| **1 大纲** | 读完源文档、**还没写 deck** 之前 | `outline 大纲.md` | `大纲-审核报告.md`：按规范 §一/§二 预检 + 人工审核要点 | **`大纲.md` 本身**：增删页、改标题、改版式、改信息点、改时长 |
| **2 画面 + 解说词** | deck 写完、解说词写完 | `deck-pdf <deck>` 和 `review-doc <deck> --version v` | `<deck>-预览.pdf`（像 PPT 一样翻）、`解说词审定稿-<版本>.md`（画面要点 / 讲述要点降序 / 解说词 / 字数 vs 目标 / lint） | deck 任意；解说词逐页文本 |
| **3 成片** | `build` 出片之后 | `qc <deck> --version v` | `质检报告.md`：实测时长与偏差、**逐页目标 vs 实际**、偏短页、音量、字幕抽样、重录勾选表 | 勾 `重录` 列标记要重录的页；调 `speed`、换参考录音 |

- **Gate 1 是事前审**：大纲没过就不要写 deck。事后补的大纲救不了结构错误。
- **Gate 2 里 deck 一改，必须重跑 `review`**：画面文字变了，解说词的数字溯源依据也跟着变。
- **Gate 3 的 `qc` 不依赖 ffprobe**：总时长读 `narration.wav`，逐页时长读 `list.ffconcat`。
- **怎么算「通过」**：owner 在审核台点「完成」时写入 `gates.json`（记录当时各文件的哈希）；
  或用 `python ppt2video.py gates <deck>` 查看状态（哪一道门、什么时候通过、之后哪些文件被改过）。
- **`build` 的硬规则**：`gates.json` 里没有 Gate ② 记录、或 deck / 解说词 / review.md 在通过之后被改过
  → **拒绝出片**。**agent 不得自行加 `--force`**；只有 owner 明确说「我知道，照出」才能用。

### 审核台（`review-ui`）

```bash
python ppt2video.py review-ui deck/index.html --version investor-14   # 四个 tab 都能用
python ppt2video.py review-ui 大纲.md                                  # 只审大纲
```

- **由 skill 在到达审核点时按需拉起**，不是常驻服务：在本机起 HTTP 服务，**前台阻塞到 owner 点「完成」**，然后写 `review-log.json`（时间 + 改了什么）并自动关闭。
- 默认只绑 `127.0.0.1`（同机自动开浏览器）；`--host <tailnet IP>` 可让别的机器也能开，`--no-open` 关掉自动开浏览器。
- 四个 tab：① 大纲 ② 画面 ③ 解说词 ④ 成片——**哪个有数据哪个可点**。
- **只读展示 + 白名单写回**：能改的只有 `大纲.md`（逐页 md 片段）、`narrations.json` 的逐页文本、`redo.json` 的重录标记。不跑 pipeline、不调 LLM。
- 保存 `大纲.md` 前把上一版留成 `.bak`；保存解说词时**当场重跑 lint**，并**同步写回 `review.md`**（build 读的是它）。
- 成片区用 **Range 请求**提供视频（进度条能拖），旁边就是逐页"目标 vs 实际"和重录勾选表。

## 多版本：同一份幻灯片，不同时长

同一份 deck 出 12 / 14 分钟两版：**改解说词详略，不动页数**。`versions.json` 放在 deck 旁边，命令加 `--version <名字>`。细节见 **`references/versions.md`**。

## 解说词规范（references/narration-spec.md）

正式口头陈述，不是聊天。核心规则（完整见 `references/narration-spec.md`）：

- **语气**：严谨（判断有依据、数字口径一致）、正向（讲进展与机会）、自信（陈述句给判断）、适度激情（每页至多一句意义点）。
- **语言**：规范书面语、只用陈述句（无问句/自问自答）、无口语俚语、无标签式引导语、不提及画面/表格/页码/左右位置、不用"首先其次最后"罗列。
- **事实与确定性**：区分已实现/进行中/规划/预测，不把低一级说成高一级；"第一""领先"要有依据并保留口径。
- **竞争对手**：只陈述可核实事实，中性词描述差异，不评价对手优劣。
- **免责**：**默认完全不出现**（deck 不加免责页，解说词不写免责/声明/风险提示）。owner 明确要求时才用 `--disclaimer` 启用，且只在 `data-disclaimer` 页、措辞固定。
- **书写格式**：数字/百分比/单位用阿拉伯数字，由 `speak()` 转读法；字幕显示书面写法。
- **配套**：`brief.md`（场景卡：受众/语气/主线/口径表/术语表/风格锚点）+ 可选 `pronounce.json`（专有名词读音）。

## 出问题时

先读 **`references/pitfalls.md`**（踩坑记录，每条都附实测数字）。常见现象速查：

| 现象 | 先查 |
|---|---|
| 某句话念得特别长 | TTS 重复惩罚（pitfalls 里 repetition_penalty 那节）|
| 每页截图都一样 | deck 不支持 `#/N` 翻页，或 `--hash-start` 设错 |
| 数字报「无出处」 | slides.json / 口径表里这个数字的写法 |
| 总时长偏差大 | `cps.json` 过期（换音色/参考音频/读音规则会自动作废）|
| 改了 deck 但截图没变 | 忘了重跑 `review` |

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

## 参考文件

| 文件 | 什么时候读 |
|---|---|
| `references/pitfalls.md` | 出问题时（每条都有实测数字）|
| `references/versions.md` | 要做多个时长版本时 |
| `references/route-b-import.md` | 走 PPTX/PDF 导入时 |
| `references/narration-spec.md` | 写/改解说词时（从属于 `business-deck-spec.md` §八）|
| `references/outline-schema.md` | 写 `大纲.md` 时（从属于 `business-deck-spec.md` §一/§二）|
| `CHANGELOG.md` | 想知道某个设计为什么是这样时 |
| `UPSTREAM.md` | 想知道与上游原版的差异时 |

