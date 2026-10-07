# my-html-ppt-video-skill (mhpvs)

把**文档**或**现成幻灯片**做成带中文配音与字幕的讲解视频。流程里有**三个人工审核点**，每道都必须停下来等人确认。

- 输入：源文档（`.md` / `.docx` / `.pdf`）或现成幻灯片（`.pptx` / `.pdf`）
- 输出：`final-*.mp4` + 字幕（烧录 / 软字幕）+ 逐页审稿文件
- 特点：克隆本人音色（mlx-audio Qwen3-TTS）、句子级 TTS 缓存、同一份 deck 出多个时长版本、解说词数字溯源 lint、本机审核台

## 三个审核点

| 门 | 何时 | 命令 | 产物 |
|---|---|---|---|
| **① 大纲** | 读完源文档、**还没做 slides** 之前 | `outline 大纲.md` | `大纲-审核报告.md`（按规范 §一/§二 预检 + 人工审核要点）|
| **② 画面 + 解说词** | deck 做完、解说词写完 | `deck-pdf` / `review-doc` / `review-ui` | 预览 PDF、解说词审定稿、审核台 |
| **③ 成片** | 出片之后 | `qc` | `质检报告.md`（时长偏差 / 逐页对比 / 音量 / 字幕抽样 / 重录表）|

`build` 前会查 `gates.json`：deck 或解说词在通过审核之后被改过就**拒绝出片**（除非 `--force`）。

## 依赖

| 必需 | 说明 |
|---|---|
| Python 3.10+ | 本仓库只依赖标准库 + `numpy` |
| ffmpeg | 用 `imageio-ffmpeg` 自带的静态二进制即可（**自带 libass**，可烧字幕），不需要 ffprobe |
| Playwright Chromium | 逐页截图（`check_deck.py` 用同一引擎，保证检查通过的画面 = 截图画面）|
| mlx-audio + Qwen3-TTS | 克隆音色（Apple Silicon）|

| 可选 | 说明 |
|---|---|
| `cn2an` | 数字的中文读法（**出片机上必需**；缺了会把数字念成阿拉伯数字）|
| PyMuPDF / python-pptx / LibreOffice | 走 PPTX/PDF 导入（路线 B）|
| `edge-tts` | 备用后端（在线，不能克隆音色）|

用 `ppt2video.py check` 一次性体检。

## 快速开始

```bash
PY=<装了依赖的 python>
P2V=ppt2video.py

$PY $P2V check                                  # 依赖体检
$PY $P2V outline 大纲.md                         # Gate ①：大纲预检 + 审核报告
$PY $P2V deck-skeleton 大纲.md                   # 大纲 → deck 骨架（每页一个 <section>）
#   做 slides：版式从 html-ppt-skill 的 layouts 里选；做完跑 check_deck.py
$PY $P2V review deck/index.html                  # 截图 + review.md
$PY $P2V narrate deck/index.html --source 原文.md # Gate ②：逐页生成解说词 + lint
$PY $P2V review-ui deck/index.html               # 审核台（可选；本机起，点"完成"退出）
$PY $P2V build deck/index.html                   # 出片
$PY $P2V qc deck/index.html                      # Gate ③：成片质检
```

已有 PPTX/PDF 时用 `import` 跳到解说词那一步，细节见 `references/route-b-import.md`。

## 端点与密钥不写进仓库

LLM 端点、文档解析服务、TTS 模型路径、参考录音都从**本地配置**读，优先级：
环境变量（`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` / `DOCREADER_URL` / `QWEN_MODEL` / `REF_AUDIO` / `REF_TEXT`）
> `~/.config/mhpvs/local.json` > 空。

```json
{
  "llm_base": "http://<你的网关>:9000/v1",
  "llm_model": "main",
  "docreader": "http://<解析服务>:50052",
  "qwen_model": "/abs/path/Qwen3-TTS-12Hz-1.7B-Base-8bit",
  "ref_audio": "/abs/path/voice.wav",
  "ref_text": "参考录音的逐字文本"
}
```

审核台绑定**非回环地址**时会自动生成访问 token 并打印带 token 的 URL；不带 token 的请求一律 403。

## 规范血缘

- **流程与密度规则**从属于《商务演示文稿制作规范》（`business-deck-spec.md`）：大纲格式、信息点 6–10、字号、逐字稿生成原则。
- **版式骨架**来自 `html-ppt-skill`（`references/layouts.md` 的骨架清单）。
- 与上游原版的差异见 `UPSTREAM.md`。

## 仓库结构

```
ppt2video.py                     唯一 CLI：check / outline / deck-skeleton / gates /
                                 import / narrate / review / deck-pdf / review-doc /
                                 qc / review-ui / build
review-ui.html                   审核台界面（单文件，无 CDN）
SKILL.md                         给 agent 看的说明书（流程 / 审核点 / 命令 / 产物）
references/
  pitfalls.md                    踩坑记录（每条都有实测数字）
  versions.md                    多时长版本
  route-b-import.md              PPTX/PDF 导入
  narration-spec.md              解说词规范与 lint 清单
  outline-schema.md              大纲格式与预检
CHANGELOG.md                     改动历史与实测记录
UPSTREAM.md                      上游来源与差异
tests/                           pytest 断言 + 小样本（调工具只动它）
```

## 测试

```bash
python -m pytest tests/ -q          # 确定性逻辑，不碰 TTS / LLM / 浏览器
```

## 许可

MIT。上游 `juguang/html-ppt-video-skill`（Copyright (c) 2026 spark）原样保留在 `upstream/`；本地化改造部分见 `LICENSE`。
