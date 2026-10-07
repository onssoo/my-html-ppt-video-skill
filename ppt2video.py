#!/usr/bin/env python3
"""ppt2video.py — HTML 幻灯片 → 配音讲解视频（M2 本地实况适配版）

适配的实际情况（2026-10-06 实测）：
  · 截图走 Playwright Chromium（`render()`）；没有 Playwright 才回退本机 Edge/Chrome
  · M2 没装 brew，但 pip 的 imageio-ffmpeg 带静态 ffmpeg 7.1 且**支持 libass**（可烧录字幕）
  · mlx-audio 的克隆接口是 Model.generate(ref_audio=, ref_text=)，**没有 generate_voice_clone**
  · HF 直连被墙 → 模型必须用**本地绝对路径**，不能用 HF 仓库 ID
  · 不设 repetition_penalty 会翻车（实测 44 字念出 32 秒）

用法（HTML 输入）：
  python ppt2video.py check
  python ppt2video.py narrate deck/index.html     # 调 LLM 端点写解说词（端点见 local.json）
  python ppt2video.py review  deck/index.html     # 截图 + 生成 review.md
  python ppt2video.py build   deck/index.html     # 配音 + 字幕 + 合成视频

用法（PPTX / PDF 输入 —— 画面已定稿，跳过 HTML PPT 那一段）：
  python ppt2video.py import  old/路演.pptx  [--]  # 渲染截图 + 生成 slides.json
  python ppt2video.py narrate old/路演.pptx --source 原文.docx --brief brief.md
  python ppt2video.py review  old/路演.pptx --reset
  python ppt2video.py build   old/路演.pptx

输入格式：
  · HTML：<section class="slide" data-title="…">，用 #/N 深链逐页截图
  · PPTX / PDF：import 一次性渲染成 slides/ 下的 PNG，其余命令只读 slides.json，
    不再关心原始格式（narrate / review / build 三条路径与 HTML 完全相同）

审核点工作文件（三个 gate，都是给人审的、确定性产物，不调 LLM）：
  python ppt2video.py outline    outline.json              # Gate 1 大纲审定稿 + 预算表
  python ppt2video.py deck-pdf   deck/index.html           # Gate 2 画面预览 PDF
  python ppt2video.py review-doc deck/index.html --version v14   # Gate 2 解说词审定稿
  python ppt2video.py qc         deck/index.html --version v14   # Gate 3 成片质检报告
"""
import argparse, asyncio, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, time, wave
import urllib.request
from collections import Counter
from html import escape as _esc
from html.parser import HTMLParser
from pathlib import Path

import numpy as np

SR = 24000                        # 统一采样率
LEAD, GAP, TAIL = 0.3, 0.25, 0.6  # 每页开头留白 / 句间停顿 / 每页结尾留白（秒）
SUB_MAX = 24                      # 每行字幕最多字数
PLACEHOLDER = "（在这里写解说词）"

# 每字耗时区间（秒/字）：判「这页讲太快/太慢」。
#   0.16 s/字 ≈ 6.3 字/秒（快）；0.50 s/字 = 2 字/秒（慢）。超出即判异常。
SEC_PER_CHAR_LO, SEC_PER_CHAR_HI = 0.16, 0.50
CPS = 4.5   # 字/秒。目标字数 = 时长(秒) × CPS（business-deck-spec §八 第 104 行）；实测 4.4–5.2，首次 build 后校准
MIN_SEC = 12        # 版本模式下每页最短时长；低于这个值一页讲不清楚，脚本会建议跳页
OVER_TOL = 1.2      # 版本模式的上限容忍：超过目标上限 20% 才判"太长"（内容优先，不硬卡上限）
DISCLAIMER = "以上财务数据为正向情景测算，不构成业绩或收益承诺，实际结果以正式披露为准。"
# 只认真正的免责句式。**不含 `情景测算`**——那是规范要求的确定性措辞
# （narration-spec §六：预测类用"按正向情景测算"），把它算进免责会误伤财务页。
DISC_WORDS = r"不构成.{0,8}(承诺|建议)|以正式披露为准|免责声明|风险提示"
# 免责语**默认关闭**（owner 2026-10-07）：除非用 --disclaimer 明确要求，
# deck 不要加免责页，解说词也不得出现任何免责/声明/风险提示类内容。
DISCLAIMER_ON = False
NO_DISC_RULE = "本页不要出现任何免责、声明、承诺、风险提示类的语句，也不要写这类内容的变体。"
OPENERS = ["首先", "其次", "最后", "那么", "好，", "好的，"]   # 只查句首（避免误伤"最后一公里""最后阶段"）
# 分类禁用词：键是类别（用于报错），值是词表。按子串匹配。
BANNED = {
    "对话与呼语": ["大家", "你会", "你看", "咱们", "我们来看", "注意，", "说白了", "说穿了"],
    "标签式套话": ["一句话", "八个字", "核心就", "结论很直接", "先说结论", "简单说", "综上所述", "值得注意的是"],
    "画面指代": ["上一页", "下一页", "这一页", "本页", "这张表", "如图", "左边", "右边", "看到的"],
    "口语俚语": ["干到", "手里的牌", "绑得很死", "难啃", "差远了", "一条龙", "拉远看", "根子", "把椅子"],
    "绝对化": ["绝对", "一层不落", "没有被撼动", "毫无疑问", "遥遥领先", "无可替代", "极强"],
}
# ─── 外部程序解析（本机实况：Edge 代替 Chrome；imageio-ffmpeg 代替系统 ffmpeg）───

BROWSER_CANDIDATES = [
    os.environ.get("BROWSER", ""),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def resolve_browser():
    for c in BROWSER_CANDIDATES:
        if c and os.path.exists(c):
            return c
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def resolve_ffmpeg():
    """优先用 imageio-ffmpeg 自带的静态二进制（实测带 libass），退回系统 ffmpeg。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


CHROME = resolve_browser()
FFMPEG = resolve_ffmpeg()

# PPTX 没有同名 PDF 时的兜底转换器。字体/特效可能与原稿有差异，见 SKILL.md「还原度」。
SOFFICE = os.environ.get("SOFFICE", "/Applications/LibreOffice.app/Contents/MacOS/soffice")

# ─── 本地配置（**不入库**）─────────────────────────────────
# 凡是跟这台机器/这个人相关的东西都走这里，仓库里只留空值与占位符：
# 端点、密钥、TTS 模型路径、参考录音及其文字。
# 优先级：环境变量 > ~/.config/mhpvs/local.json > 空
#   示例：{"llm_base": "http://<网关>:9000/v1", "llm_model": "main",
#          "docreader": "http://<解析服务>:50052",
#          "qwen_model": "/abs/path/Qwen3-TTS-...-8bit",
#          "ref_audio": "/abs/path/voice.wav", "ref_text": "参考录音的逐字文本"}
LOCAL_CONF = Path(os.environ.get("MHPVS_CONF", "~/.config/mhpvs/local.json")).expanduser()


def local_conf():
    try:
        return json.loads(LOCAL_CONF.read_text(encoding="utf-8"))
    except Exception:
        return {}


_CONF = local_conf()
DEFAULT_LLM_BASE = os.environ.get("LLM_BASE_URL") or _CONF.get("llm_base", "")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL") or _CONF.get("llm_model", "main")

# ─── 默认配置 ────────────────────────────────────────────
# 注意：qwen_model 必须是**本地绝对路径**——用 HF 仓库 ID 会触发联网下载，而 HF 被墙。
DEFAULTS = {
    "backend": "qwen",            # qwen / edge
    "qwen_model": os.environ.get("QWEN_MODEL") or _CONF.get("qwen_model", ""),
    # —— 克隆模式（backend=qwen 且填了 ref_audio 时启用）——
    "ref_audio": os.environ.get("REF_AUDIO") or _CONF.get("ref_audio", ""),
    "ref_text": os.environ.get("REF_TEXT") or _CONF.get("ref_text", ""),
    # —— 预置音色模式（ref_audio 为空时启用）——
    "speaker": "Vivian",          # Vivian / Serena / Uncle_Fu / Dylan / Eric
    "instruct": "",
    # —— edge-tts 备用 ——
    "edge_voice": "zh-CN-YunxiNeural",
    "edge_rate": "+5%",
    "subtitles": "burn",          # burn（烧录）/ soft（软字幕）/ off
    # 单版本模式的每页字数区间（下限用于卡"偏短"，封面/目录/章节/结尾页见 page_floor 的豁免）
    "length": "100-180",
    "speed": 1.15,                # 变速（ffmpeg atempo，不变调）。模型自带的 speed 参数在克隆模式下无效，只能后处理
    # —— 画面构图（导入的旧 PPTX/PDF 没有预留字幕区时用 letterbox）——
    "frame": "fit",               # fit：铺满画面，居中留边；letterbox：画面上移，底部留 120px 给字幕
    "pad_color": "black",         # 留边颜色，可写成与幻灯片底色一致，如 0x0B1F3A
}

SOURCE_MAX = int(os.environ.get("SOURCE_MAX_CHARS", "60000"))

# 文档解析服务（把 docx/pdf 等转文本）。端点同样只放本地配置。
DOCREADER = os.environ.get("DOCREADER_URL") or _CONF.get("docreader", "")

NARRATE_SYSTEM = """你是正式商务视频的解说撰稿人。解说词由配音朗读，观众是投资人和产业合作方。

【语气】严谨、正向、自信，适度体现创业者的投入感。听感接近一位熟悉业务的负责人在正式场合做介绍：措辞规范，判断明确且有依据，对前景有信心，同时如实说明前提条件。如【场景卡】提供了风格样本，模仿其用词习惯、句式长短和节奏，不照搬内容。

【语言】
1. 规范书面语，适合朗读。单句一般 15 到 40 字，可用因果、转折、递进等复句，不要连续堆砌短句造成电报体。
2. 只用陈述句。不用问句，不自问自答，不设悬念。
3. 不称呼观众，不用"大家""你""咱们"，不用"好""注意""说白了"等口头语。
4. 不用口语俚语和比喻，例如"手里的牌""干到""绑得很死""难啃""一条龙"。
5. 不用"一句话""八个字""核心就是""先说结论"等标签式引导语。
6. 不提及画面、表格、页码和左右位置。解说词要独立成立，不看画面也能听懂。
7. 不用"首先、其次、最后"。并列行动项可用"第一、第二、第三"，不超过三项。
8. 术语首次出现写成"缩写+中文名"，以【场景卡】的术语表为准。

【结构】
- 先给出本页的核心判断，再说明依据，必要时用一句话点明它对公司或行业的意义。不要每页套用同一句式。
- 同一章节内直接进入内容，不写过渡句。只有本页开启新章节时，才用一句话说明与前文的逻辑关系。不预告后续内容，不复述上一页。
- 第一页说明本次介绍要回答的问题和核心结论；最后一页回到这个问题，给出结论和关键时间节点。

【事实与分寸】
- 事实和数字只能来自【当前页】【源文档相关段落】和【场景卡】的口径表。口径表中有的数字，必须使用口径表的写法。
- 按确定性选择措辞：已实现的直接陈述；进行中的用"正在""目前处于"；规划用"计划""预计"；预测用"按正向情景测算""目标""有望"。
- "第一""领先"必须有依据并保留口径。不用"绝对""遥遥领先""无可替代""极强"。
- 提到竞争对手只陈述客观事实，用中性词描述差异，不评价对手优劣。
- 激情只体现在对意义和方向的清晰表达上，全页最多一句，不用感叹号和口号。
- {disclaimer}

【书写格式】数字、百分比、单位用阿拉伯数字和规范写法，例如 7300 万元、92%、G8.6、2026 年。英文缩写保持原样。不用括号、斜杠、箭头、破折号、引号和 Markdown。

【场景卡】
{brief}

只输出解说词正文，不要标题、引号或任何说明。"""


# ─── 幻灯片解析与截图 ─────────────────────────────────────

class SlideParser(HTMLParser):
    """提取每个 <section class="slide"> 的标题、时长（data-sec）、画面文字和讲述要点（.notes）。"""
    def __init__(self):
        super().__init__()
        self.slides, self.depth, self.skip = [], 0, 0
        self.in_notes = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "section":
            if self.depth == 0 and "slide" in (a.get("class") or "").split():
                self.depth = 1
                self.slides.append({"title": (a.get("data-title") or "").strip(),
                                   "cls": (a.get("class") or "").strip(),
                                    "sec": int(a["data-sec"]) if (a.get("data-sec") or "").isdigit() else None,
                                    "fixed": a.get("data-fixed"),
                                    "disc": "data-disclaimer" in a,
                                    "text": [], "notes": []})
            elif self.depth:
                self.depth += 1
        if tag in ("div", "section") and "notes" in (a.get("class") or "").split():
            self.in_notes += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in ("div", "section") and self.in_notes:
            self.in_notes -= 1
        if tag == "section" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip and data.strip():
            (self.slides[-1]["notes"] if self.in_notes else self.slides[-1]["text"]).append(data.strip())


def extract(deck):
    p = SlideParser()
    p.feed(Path(deck).read_text(encoding="utf-8"))
    return p.slides


def write_outline(out, slides):
    blocks = []
    for i, s in enumerate(slides, 1):
        sec = f"（{s['sec']} 秒）" if s.get("sec") else ""
        md = f"## 第{i}页 {s['title']}{sec}\n" + "\n".join(s["text"])
        if s.get("notes"):
            md += "\n\n讲述要点：\n" + "\n".join(f"- {n}" for n in s["notes"])
        blocks.append(md)
    text = "\n\n".join(blocks)
    Path(out, "outline.md").write_text(text, encoding="utf-8")
    return text


def render(deck, out, n, start):
    """截图与 check_deck 用同一个引擎（Playwright Chromium，msedge 回退），保证检查通过的画面 = 截图画面。"""
    from playwright.sync_api import sync_playwright
    d = Path(out, "slides")
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.png"):                       # 清掉多余的旧图（页数变少时）
        if f.stem.isdigit() and int(f.stem) > n:
            f.unlink()
    uri = Path(deck).as_uri()
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            print("（自带 Chromium 不可用，回退本机 Edge/msedge）", file=sys.stderr)
            b = p.chromium.launch(channel="msedge")
        pg = b.new_page(viewport={"width": 1920, "height": 1080})   # 舞台缩放比例 = 1
        for i in range(1, n + 1):
            png = d / f"{i:03d}.png"                                # 3 位，避免 >99 页排序错乱
            ok = False
            for attempt in range(3):
                pg.goto("about:blank")
                pg.goto(f"{uri}#/{i - 1 + start}")
                pg.wait_for_load_state("networkidle")
                pg.evaluate("document.fonts.ready.then(() => true)")   # 等字体加载完
                pg.wait_for_timeout(600)                                # 等翻页过渡结束
                pg.screenshot(path=str(png))
                if png.exists() and png.stat().st_size > 0:
                    ok = True
                    break
                print(f"    ↻ 第 {i} 页截图为空，重试 {attempt + 1}/3")
            if not ok:
                b.close()
                sys.exit(f"第 {i} 页截图失败（检查浏览器能否用 #/N 翻页，reveal.js 试 --hash-start 0）")
            print(f"  ✔ slides/{png.name}")
    _hs = [hashlib.sha1((d / f"{x:03d}.png").read_bytes()).hexdigest() for x in range(1, n + 1)]
    _dup = [x for x in range(2, n + 1) if _hs[x - 1] == _hs[x - 2]]
    if len(set(_hs)) == 1 and n > 1:
        sys.exit("所有截图完全相同：deck 不支持 #/N 翻页，或 --hash-start 设错（reveal.js 试 0）")
    if _dup:
        print(f"  ⚠ 第 {_dup} 页与前一页截图完全相同，检查翻页")
        b.close()


# ─── PPTX / PDF 导入（跳过 HTML PPT 那一段：画面已定稿）─────
# 依赖：pip install pymupdf python-pptx
#   · PDF  ：PyMuPDF 逐页渲染成 PNG + 提取文字（PDF 里没有演讲者备注）
#   · PPTX ：python-pptx 提取标题/文字/表格/图表数据/演讲者备注；截图走同名 PDF，
#            没有同名 PDF 时才用 LibreOffice 转（字体与特效可能有差异）
# 中间文件 slides.json 可以手工编辑 sec（目标秒数）/ disc（是否要免责语，默认关闭）/ notes（讲述要点），
# 重新导入时会保留这三次改动。

def pdf_pages(pdf, out):
    """PDF 逐页渲染成 PNG（按原比例放进 1920×1080，不裁剪），并提取标题和文字。"""
    try:
        import fitz
    except ImportError:
        sys.exit("需要 PyMuPDF：pip install pymupdf")
    fitz.TOOLS.mupdf_display_errors(False)             # LibreOffice 导出的 PDF 会刷 structure tree 噪音
    d = Path(out, "slides")
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.png"):
        f.unlink()                                     # 重新导入时清掉上一版截图
    pages = []
    with fitz.open(pdf) as doc:
        for i, pg in enumerate(doc, 1):
            z = min(1920 / pg.rect.width, 1080 / pg.rect.height)
            pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).save(str(d / f"{i:03d}.png"))
            top = [s for b in pg.get_text("dict")["blocks"] for l in b.get("lines", [])
                   for s in l["spans"] if s["text"].strip() and s["bbox"][1] < pg.rect.height * 0.3]
            title = max(top, key=lambda s: s["size"])["text"].strip() if top else ""
            text = [x.strip() for x in pg.get_text().splitlines() if x.strip()]
            pages.append({"title": title, "text": text})
    print(f"  ✔ 渲染 {len(pages)} 页 → {d}")
    return pages


def pptx_to_pdf(pptx, out):
    """优先用同名 PDF（放在 PPTX 旁边、且比它新）；其次复用上次转换出来的 PDF；都没有才调 LibreOffice。"""
    same = Path(pptx).with_suffix(".pdf")
    cands = [same, Path(out, Path(pptx).stem + ".pdf")]
    fresh = [c for c in cands if c.exists() and c.stat().st_mtime >= Path(pptx).stat().st_mtime]
    if fresh:
        pick = max(fresh, key=lambda c: c.stat().st_mtime)
        print(f"  使用已有 PDF：{pick.name}")
        return pick
    exe = SOFFICE if os.path.exists(SOFFICE) else shutil.which("soffice")
    if not exe:
        sys.exit("没有同名 PDF，也找不到 LibreOffice。建议用 PowerPoint/Keynote 导出同名 PDF 放在旁边")
    print("  用 LibreOffice 转换 PDF（字体和特效可能与原稿有差异）…")
    r = subprocess.run([exe, "-env:UserInstallation=file:///tmp/lo_ppt2video", "--headless",
                        "--convert-to", "pdf", "--outdir", str(out), str(pptx)],
                       capture_output=True, text=True)
    pdf = Path(out, Path(pptx).stem + ".pdf")
    if not pdf.exists():          # LibreOffice 常把告警写到 stderr；判据是产物存在，不是返回码
        sys.exit(f"LibreOffice 转换失败：{r.stdout[-300:]}{r.stderr[-300:]}")
    return pdf


def pptx_slides(path):
    """提取每页标题、文字、表格、图表数据和演讲者备注（跳过隐藏页）。"""
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError:
        sys.exit("需要 python-pptx：pip install python-pptx")

    def texts(shapes):
        for sh in shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from texts(sh.shapes)
            elif sh.has_text_frame and sh.text_frame.text.strip():
                yield sh.text_frame.text.strip()
            elif getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    yield " | ".join(c.text.strip() for c in row.cells)
            elif getattr(sh, "has_chart", False) and sh.has_chart:
                try:                                   # 图表里的数字也提取出来，供数字溯源使用
                    for plot in sh.chart.plots:
                        cats = list(plot.categories)
                        for ser in plot.series:
                            # 整数不要写成 7300.0：否则解说词写"7300"会被数字溯源误判为无出处
                            vals = [int(v) if isinstance(v, float) and v.is_integer() else v
                                    for v in ser.values]
                            yield f"{ser.name}：" + "，".join(f"{c} {v}" for c, v in zip(cats, vals))
                except Exception as e:
                    print(f"    ⚠ 有一张图表没提取到数据（{type(e).__name__}），请人工核对")

    slides = []
    for sl in Presentation(path).slides:
        if sl._element.get("show") == "0":             # 隐藏页
            continue
        t = sl.shapes.title
        title = t.text_frame.text.strip() if t is not None and t.has_text_frame else ""
        nf = sl.notes_slide.notes_text_frame if sl.has_notes_slide else None
        notes = nf.text.strip() if nf is not None else ""
        slides.append({"title": title, "sec": None, "fixed": None, "disc": False,
                       "text": [x for x in texts(sl.shapes) if x != title],
                       "notes": [notes] if notes else []})
    return slides


def inherit_prev(slides, old):
    """重新导入时继承上次手工改过的 sec / fixed / disc / notes。

    规则（都是实测踩过的坑）：
      1. **重名标题不参与**按标题匹配——多个"目录"、多个「（续）」页会互相串；
      2. 每份旧设置**只继承一次**（used 集合），不会被两页同时拿走；
      3. **页数变了就不按页码兜底**——插页会让后面每页整体错位；
    返回 (按标题命中数, 按页码命中数, 没继承到的页码列表)。"""
    tt = lambda o: (o.get("title") or "").strip()
    cnt = Counter(tt(o) for o in old)
    uniq = {tt(o): k for k, o in enumerate(old) if tt(o) and cnt[tt(o)] == 1}
    used, got = set(), {}
    for k, s in enumerate(slides):                     # 第一轮：标题唯一且相同
        m = uniq.get(s["title"].strip())
        if m is not None and m not in used:
            got[k] = m; used.add(m)
    n_title = len(got)
    if len(old) == len(slides):                        # 第二轮：页数没变才按页码补
        for k in range(len(slides)):
            if k not in got and k not in used:
                got[k] = k; used.add(k)
    for k, m in got.items():
        o, s = old[m], slides[k]
        s["sec"], s["fixed"], s["disc"] = o.get("sec"), o.get("fixed"), o.get("disc", False)
        if not s["notes"] and o.get("notes"):
            s["notes"] = o["notes"]
    miss = [k + 1 for k in range(len(slides)) if k not in got]
    return n_title, len(got) - n_title, miss


def cmd_import(deck, out):
    p, sj = Path(deck), Path(out, "slides.json")
    suf = p.suffix.lower()
    if suf == ".pptx":
        slides = pptx_slides(p)
        pages = pdf_pages(pptx_to_pdf(p, out), out)
        if len(slides) != len(pages):
            sys.exit(f"PPTX 有 {len(slides)} 页可见幻灯片，PDF 有 {len(pages)} 页，无法一一对应。\n"
                     f"  常见原因：① LibreOffice 把隐藏页也导出了（请用 PowerPoint 自己导出 PDF）\n"
                     f"  ② 同名 PDF 是旧版本 ③ 幻灯片版式异常。请处理后重试")
        for s, pg in zip(slides, pages):
            s["title"] = s["title"] or pg["title"]
    elif suf == ".pdf":
        slides = [{"title": pg["title"], "sec": None, "fixed": None, "disc": False,
                   "text": pg["text"], "notes": []}
                  for pg in pdf_pages(p, out)]
    elif suf == ".ppt":
        sys.exit("不支持旧版 .ppt，请先另存为 .pptx 或导出 PDF")
    else:
        sys.exit(f"不支持导入 {suf or '（无扩展名）'}，只支持 .pptx / .pdf")

    if sj.exists():                                    # 保留上次手工改过的内容
        try:
            old = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            old = []
        if len(old) != len(slides):
            print(f"⚠ 页数有变化（{len(old)} → {len(slides)}），已按页码保留旧的 sec / disc / notes，请核对")
        n_title, n_page, miss = inherit_prev(slides, old)
        print(f"  继承上次改动：按标题 {n_title} 页，按页码 {n_page} 页")
        if miss:
            print(f"  ⚠ 第 {miss} 页没有继承（新页、改了标题或页数变化），请检查 sec / notes")
    empty = [i for i, s in enumerate(slides, 1) if not s["text"]]
    noted = sum(bool(s["notes"]) for s in slides)
    print(f"已写入 {sj}：{len(slides)} 页，{noted} 页有备注"
          + (f"；第 {empty} 页没有提取到文字（可能是纯图片），建议补 notes 或加 --vision" if empty else ""))
    print("下一步：检查 slides/ 下的截图；在 slides.json 里设置 sec / disc / notes，然后跑 narrate")


def load_slides(deck, out):
    """HTML 走 DOM 解析；PPTX/PDF 走 slides.json（不存在或比源文件旧时自动 import）。"""
    if Path(deck).suffix.lower() in (".html", ".htm"):
        return extract(deck)
    sj = Path(out, "slides.json")
    if not sj.exists() or sj.stat().st_mtime < Path(deck).stat().st_mtime:
        cmd_import(deck, out)
    slides = json.loads(sj.read_text(encoding="utf-8"))
    for s in slides:                                   # 允许在 JSON 里把 notes 写成字符串
        if isinstance(s.get("notes"), str):
            s["notes"] = [s["notes"]] if s["notes"].strip() else []
        s.setdefault("title", ""); s.setdefault("sec", None); s.setdefault("fixed", None)
        s.setdefault("disc", False); s.setdefault("text", []); s.setdefault("notes", [])
    return slides


# ─── review.md ───────────────────────────────────────────

def set_review_cfg(rv, **kv):
    """在 review.md 的 YAML 头里更新/追加配置项（不动正文与解说词）。"""
    p = Path(rv)
    txt = p.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", txt, re.S)
    if not m:
        return False
    head = m.group(1).splitlines()
    for k, val in kv.items():
        for i, line in enumerate(head):
            if line.split(":", 1)[0].strip() == k:
                head[i] = f"{k}: {val}"
                break
        else:
            head.append(f"{k}: {val}")
    p.write_text("---\n" + "\n".join(head) + "\n---\n" + txt[m.end():], encoding="utf-8")
    return True


def write_review(path, slides, narr, cfg, skip=(), slides_dir=None):
    d = Path(path).parent
    sdir = Path(slides_dir) if slides_dir else d / "slides"
    lines = ["---", *[f"{k}: {v}" for k, v in cfg.items()], "---", "",
             "<!-- 改代码块里的解说词；[ ] 改成 [x] 跳过该页；",
             "     backend: qwen / edge ；subtitles: burn / soft / off",
             "     frame: fit / letterbox ；pad_color: black 或 0x0B1F3A",
             "     ref_audio 填本地录音路径 = 克隆你的声音；清空则用 speaker 预置音色 -->", ""]
    for i, s in enumerate(slides, 1):
        title = s["title"] or (s["text"][0][:20] if s["text"] else f"Slide {i}")
        mark = "x" if i in skip else " "                 # 版本里 skip 掉的页默认勾上
        rel = os.path.relpath(sdir / f"{i:03d}.png", d)  # 版本目录下要回退两级，必须算相对路径
        lines += [f"# {i} · {title}", "", f"- [{mark}] 跳过此页", "", f"![]({rel})", "",
                  "```", narr.get(str(i), PLACEHOLDER), "```", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def parse_review(path):
    txt = Path(path).read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", txt, re.S)
    cfg = dict(DEFAULTS)
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip().strip("\"'")
    pages = []
    for blk in re.split(r"^# (?=\d+ · )", txt[m.end():], flags=re.M)[1:]:
        num = int(blk.split(" ", 1)[0])
        skip = re.search(r"^- \[[xX]\]", blk, re.M) is not None
        code = re.search(r"```\n(.*?)\n```", blk, re.S)
        pages.append((num, skip, code.group(1).strip() if code else ""))
    return cfg, pages


# ─── 音频工具 ────────────────────────────────────────────

def to_wav(src, dst):
    if not FFMPEG:
        sys.exit("找不到 ffmpeg（试 `pip install imageio-ffmpeg`）")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
                    "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(dst)], check=True)


def write_wav(path, a, sr=SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(a.astype("<i2").tobytes())


def read_wav_sped(path, speed, cache_dir):
    """按 speed 变速读取（ffmpeg atempo，保音高）。结果按文件名缓存，改速度只重跑 ffmpeg。"""
    if abs(speed - 1.0) < 1e-3:
        return read_wav(path)
    out = Path(cache_dir) / f"{Path(path).stem}_x{speed:.2f}.wav"
    if not out.exists():
        if not FFMPEG:
            sys.exit("变速需要 ffmpeg（pip install imageio-ffmpeg）")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(path),
                        "-filter:a", f"atempo={speed}", "-ar", str(SR), "-ac", "1",
                        "-sample_fmt", "s16", str(out)], check=True)
    return read_wav(out)


def read_wav(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")



def _file_hash(path, chunk=1 << 20):
    """文件内容 sha1（前 16 位）。用于缓存键，避免同名不同内容被误判为同一文件。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()[:16]

class QwenTTS:
    """两种模式：
       · 克隆（cfg['ref_audio'] 非空）→ Model.generate(ref_audio=, ref_text=)
       · 预置音色                     → Model.generate_custom_voice(speaker=)
    """
    def __init__(self, cfg):
        self.cfg, self.model, self.sr = cfg, None, SR
        self.ref_audio = (cfg.get("ref_audio") or "").strip()
        self.ref_text = (cfg.get("ref_text") or "").strip()
        self.clone = bool(self.ref_audio)
        if self.clone:
            if not self.ref_text:
                sys.exit("克隆模式必须填 ref_text（录音的文字，一字不差）")
            if not os.path.exists(self.ref_audio):
                sys.exit(f"参考音频不存在：{self.ref_audio}")
            self.kw = {"ref_audio": self.ref_audio, "ref_text": self.ref_text,
                       "lang_code": "Chinese"}
            # 克隆模式**忽略 instruct**：克隆走 batch_generate，它只收复数 instructs，
            # 传单数会 ValueError（然后整批退回逐句，白白变慢）。要调语气请换参考录音。
            if cfg.get("instruct"):
                print(f"⚠ 克隆模式不支持 instruct，已忽略：{str(cfg['instruct'])[:40]}"
                      "（语气由参考录音决定）")
            # 缓存键带参考音频**内容**哈希：换参考音频（哪怕同名覆盖）才会重建缓存，
            # 否则旧音频的缓存会被错误复用。
            self.key = ["qwen-clone", cfg["qwen_model"], self.ref_audio,
                        _file_hash(self.ref_audio), self.ref_text]
        else:
            self.kw = {"speaker": cfg["speaker"], "language": "Chinese"}
            if cfg.get("instruct"):
                self.kw["instruct"] = cfg["instruct"]
            self.key = ["qwen-preset", cfg["qwen_model"], cfg["speaker"], cfg.get("instruct", "")]

    # 一致性优先的采样参数：官方 generation_config 是 T=0.9/top_p=1.0/top_k=50，
    # 但每句独立采样会让音色句间漂移（用户反馈"声音波动"）。降到 0.7/30 减小方差。
    SAMPLING = {"temperature": 0.7, "top_k": 30, "top_p": 1.0, "repetition_penalty": 1.05}

    def _ensure_model(self):
        if self.model is None:
            from mlx_audio.tts.utils import load_model
            print(f"  加载 {self.cfg['qwen_model']} …")
            self.model = load_model(Path(self.cfg["qwen_model"]))
            self.sr = getattr(self.model, "sample_rate", SR)

    def batch(self, texts):
        """一次前向处理多句 —— 共享同一份参考条件，句间音色才稳定。"""
        self._ensure_model()
        kw = dict(self.kw, **self.SAMPLING)
        # batch_generate 的参数是复数形式：instructs 是**列表**，不接受单数 instruct
        ins = kw.pop("instruct", None)
        if ins:
            kw["instructs"] = [ins] * len(texts)
        gen = self.model.batch_generate(texts=list(texts), **kw)
        out = []
        for r in gen:
            a = np.asarray(r.audio, dtype=np.float32).reshape(-1)
            out.append(a)
        if len(out) != len(texts):
            raise RuntimeError(f"batch_generate 返回 {len(out)} 段，期望 {len(texts)} 段")
        return out

    def synth(self, text, dst):
        self._ensure_model()
        n = max(1, len(re.sub(r"\W", "", text)))
        lo, hi = SEC_PER_CHAR_LO * n, SEC_PER_CHAR_HI * n
        a, best = None, None
        for attempt in range(4):
            rep = 1.05 + 0.08 * attempt             # ★ 逐次加大重复惩罚（原脚本重试参数不变 → 必然同样翻车）
            kw = dict(self.kw, **self.SAMPLING)
            kw["repetition_penalty"] = rep      # 覆盖 SAMPLING 里的默认，避免重复关键字
            if self.clone:
                res = list(self.model.generate(text=text, **kw))
            else:
                res = list(self.model.generate_custom_voice(text=text, **kw))
            a = np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in res])
            d = len(a) / self.sr
            if lo <= d <= hi:
                best = a
                break
            if best is None or abs(d - (lo + hi) / 2) < abs(len(best) / self.sr - (lo + hi) / 2):
                best = a                              # 全都不合格时留最接近的
            print(f"    ⚠ 时长异常 {d:.1f}s（合理 {lo:.1f}~{hi:.1f}s）rep={rep:.2f} 重试")
        a = best
        write_wav(dst, (np.clip(a, -1, 1) * 32767).astype(np.int16), self.sr)
        if self.sr != SR:
            to_wav(dst, dst)


class EdgeTTS:
    def __init__(self, cfg):
        import edge_tts
        self.edge, self.voice, self.rate = edge_tts, cfg["edge_voice"], cfg["edge_rate"]
        self.key = ["edge", self.voice, self.rate]

    def synth(self, text, dst):
        tmp = f"{dst}.mp3"
        asyncio.run(self.edge.Communicate(text, self.voice, rate=self.rate).save(tmp))
        to_wav(tmp, dst)
        os.remove(tmp)


def synth_retry(tts, text, path, tries=3):
    """TTS 是网络/推理调用：实测被瞬时抖动打断过（edge 的 NoAudioReceived、网关 reset）。
    有限次退避重试；仍失败就抛出——不吞错，也不把失败的产物留在缓存里。"""
    for k in range(1, tries + 1):
        try:
            tts.synth(text, path)
            if path.exists() and path.stat().st_size > 0:
                return
            raise RuntimeError("产出是空文件")
        except Exception as e:
            if k == tries:
                raise
            wait = 2 ** k
            print(f"    ↻ 合成失败（{type(e).__name__}: {str(e)[:60]}），{wait}s 后重试 {k}/{tries - 1}")
            time.sleep(wait)


def cache_key(tts, text):
    """一句音频的缓存键：与音色、后端、语速、读音规则、参考录音内容绑定。"""
    return hashlib.sha1(json.dumps(tts.key + [text], ensure_ascii=False).encode()).hexdigest()[:16]


def cached_many(tts, texts, cache):
    """返回 texts 对应的 wav 路径列表；未命中的句子**一次 batch** 合成。"""
    paths, miss = [], []
    for t in texts:
        h = cache_key(tts, t)
        p = cache / f"{h}.wav"
        paths.append(p)
        if not p.exists():
            miss.append((t, p))
    if miss:
        print(f"    batch 合成 {len(miss)} 句（共 {len(texts)} 句，{len(texts)-len(miss)} 句命中缓存）")
        try:
            audios = tts.batch([t for t, _ in miss])
        except Exception as e:
            print(f"    ⚠ batch 失败（{type(e).__name__}: {str(e)[:80]}），退回逐句")
            for t, p in miss:
                synth_retry(tts, t, p)
        else:
            for (t, p), a in zip(miss, audios):
                write_wav(p, (np.clip(a, -1, 1) * 32767).astype(np.int16), tts.sr)
    return paths


def cached(tts, text, cache):
    h = cache_key(tts, text)
    p = cache / f"{h}.wav"
    if not p.exists():
        synth_retry(tts, text, p)
    return p


# ─── 字幕 ────────────────────────────────────────────────

def sentences(text):
    text = re.sub(r"\s+", " ", text.strip())
    return [s.strip() for s in re.findall(r"[^。！？；!?;…]+[。！？；!?;…]*", text) if s.strip()]


def split_cue(s, start, end):
    """长句按逗号拆成多条字幕，时间按字数比例分配。"""
    s = s.rstrip("。！？；!?;…，,")
    chunks, buf = [], ""
    for piece in re.split(r"(?<=[，、,：:])", s):
        if buf and len(buf) + len(piece) > SUB_MAX:
            chunks.append(buf); buf = piece
        else:
            buf += piece
    if buf:
        chunks.append(buf)
    parts = []
    for c in chunks:
        while len(c) > SUB_MAX:
            parts.append(c[:SUB_MAX]); c = c[SUB_MAX:]
        if c:
            parts.append(c)
    total = sum(map(len, parts)) or 1
    cues, t = [], start
    for c in parts:
        d = (end - start) * len(c) / total
        cues.append((t, t + d, c.rstrip("，、,：:")))
        t += d
    return cues


def ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"



# ─── 书面写法 → 口语读法（字幕保留书面，配音读口语）────────
PRON_FILE = Path(os.environ.get("PRONOUNCE", "pronounce.json"))
PRON = json.loads(PRON_FILE.read_text(encoding="utf-8")) if PRON_FILE.exists() else {}


def pron_hash():
    """读音表也算语速指纹的一部分：改读音会改变音频长度。"""
    return hashlib.sha1(json.dumps(PRON, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]


def load_pron(out=None, deck=None):
    """读音表优先级：输出目录 > deck 同目录 > PRONOUNCE 环境变量 > 当前目录。"""
    for c in (Path(out, "pronounce.json") if out else None,
              Path(deck).parent / "pronounce.json" if deck else None,
              PRON_FILE if os.environ.get("PRONOUNCE") else None):
        if c and Path(c).exists():
            try:
                PRON.update(json.loads(Path(c).read_text(encoding="utf-8")))
                print(f"  读音表：{c}（共 {len(PRON)} 条）")
                return
            except Exception as e:
                print(f"  ⚠ 读不了读音表 {c}：{type(e).__name__}")
# 单位/范围 → 读法（先于 cn2an，避免 cn2an 把 "ms" 当字母读）。
# 用 (?![A-Za-z]) 而非 \b：数字后面常跟中文（"120ms降"），\b 在 CJK 前不成立。
# 金额单位：万元到了"亿"的量级就折算成亿元（100000 万元 → 10亿元 → 十亿元）。
# 千万级及以下保持"万元"，由 cn2an 读成"七千三百万元"——那本来就是中文的自然说法。
_WAN = r"(\d[\d,]*(?:\.\d+)?)"
_WAN_RANGE = re.compile(_WAN + r"\s*[–\-~～至]\s*" + _WAN + r"\s*万元")
_WAN_ONE = re.compile(_WAN + r"\s*万元")


def _yi_label(raw):
    """万元数值 → 亿元写法；不到 1 亿返回 None（表示保持原样）。"""
    try:
        v = float(raw.replace(",", ""))
    except ValueError:
        return None
    if v < 10000:
        return None
    return f"{v / 10000:.4f}".rstrip("0").rstrip(".") + "亿元"


def _wan_range_sub(m):
    a, b = _yi_label(m.group(1)), _yi_label(m.group(2))
    if not a and not b:
        return m.group(0)
    return f"{a or m.group(1).replace(',', '') + '万元'}到{b or m.group(2).replace(',', '') + '万元'}"


# 年份必须**逐位**读："2026 年" → "二零二六年"。不处理的话 cn2an 会当数量念成"二千零二十六年"。
# 只认 19xx/20xx/21xx，避免把 "1200 年历史" 这类数量误读成"一二零零年"。
# 这两条必须排在区间规则之前，否则 "2026–2030 年" 会先被拆成 "2026到2030 年"。
_D = "零一二三四五六七八九"
_digits = lambda s: "".join(_D[int(c)] for c in s)
_YEAR_ONE = re.compile(r"((?:19|20|21)\d{2})\s*年")
_YEAR_RANGE = re.compile(r"((?:19|20|21)\d{2})\s*[–\-~～至]\s*((?:19|20|21)\d{2})\s*年")
_CN2AN_WARNED = False
_SPEAK_RULES = [
    # 日期与尺寸要排在通用区间规则之前，否则 2026-10-07 会被当成区间读成"到"
    (re.compile(r"(?<!\d)((?:19|20|21)\d{2})-(\d{1,2})-(\d{1,2})(?!\d)"),
     lambda m: f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"),
    (re.compile(r"(\d+)\s*[xX×]\s*(\d+)"), r"\1乘\2"),
    (_WAN_RANGE, _wan_range_sub),                       # 金额规则要在通用区间规则之前
    (_WAN_ONE, lambda m: _yi_label(m.group(1)) or m.group(0)),
    (_YEAR_RANGE, lambda m: f"{_digits(m.group(1))}年到{_digits(m.group(2))}年"),
    (_YEAR_ONE, lambda m: f"{_digits(m.group(1))}年"),
    (re.compile(r"(\d)\s*[–\-~～]\s*(\d)"), r"\1到\2"),
    (re.compile(r"(\d)\s*ms(?![A-Za-z])"), r"\1毫秒"),
    (re.compile(r"(\d)\s*[xX×](?![A-Za-z])"), r"\1倍"),
    (re.compile(r"(\d)\s*s(?![A-Za-z])"), r"\1秒"),
    (re.compile(r"(\d)\s*ppm(?![A-Za-z])"), r"\1 ppm"),
    (re.compile(r"(\d)\s*ppb(?![A-Za-z])"), r"\1 ppb"),
]


def speak(text):
    """书面写法 → 口语读法：先查读音表（长词优先），再套单位规则，最后 cn2an 转数字/百分比。
    字幕仍用原句，只有送进 TTS 的文本走这里。"""
    for k in sorted(PRON, key=len, reverse=True):
        text = text.replace(k, PRON[k])
    for pat, rep in _SPEAK_RULES:
        text = pat.sub(rep, text)
    try:
        import cn2an
        return cn2an.transform(text, "an2cn")
    except Exception as e:
        global _CN2AN_WARNED
        if not _CN2AN_WARNED:                # 只报一次，否则每句数字都刷屏
            _CN2AN_WARNED = True
            print("    ⚠ 本机没装 cn2an：数字不做中文读法转换（保留阿拉伯数字）。"
                  "出片机上装了就没问题。")
        return text


def has_libass():
    if not FFMPEG:
        return False
    r = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True)
    return re.search(r"^\s*\S+\s+subtitles\s", r.stdout, re.M) is not None


# ─── 命令 ────────────────────────────────────────────────

def cmd_check():
    ok = True
    def row(good, name, hint, required=True):
        nonlocal ok
        ok &= good or not required
        print(f"  {'✔' if good else ('✘' if required else '–')} {name}" + ("" if good else f"  → {hint}"))

    row(FFMPEG is not None, "ffmpeg", "pip install imageio-ffmpeg")
    if FFMPEG:
        print(f"      {FFMPEG}")
        row(has_libass(), "烧录字幕（libass）", "不支持，将自动改用软字幕", required=False)
    _pw = importlib.util.find_spec("playwright") is not None
    row(_pw, "截图引擎（Playwright）", "pip install playwright && playwright install chromium")
    row(CHROME is not None, "本机浏览器（仅备用）", "可不装；没 Playwright 时才会用", False)
    if CHROME:
        print(f"      {CHROME}")
    row(importlib.util.find_spec("mlx_audio") is not None, "mlx-audio", "pip install mlx-audio", False)
    row(importlib.util.find_spec("edge_tts") is not None, "edge-tts", "pip install edge-tts", False)
    row(importlib.util.find_spec("numpy") is not None, "numpy", "pip install numpy")
    row(importlib.util.find_spec("cn2an") is not None, "cn2an（数字中文读法）",
        "pip install cn2an —— 只在**出片机**上必需；缺了 TTS 会把数字念成阿拉伯数字", False)
    # ★ 导入 PPTX/PDF 才需要（HTML 流程不依赖）
    row(importlib.util.find_spec("fitz") is not None, "pymupdf（PPTX/PDF 导入）",
        "pip install pymupdf", required=False)
    row(importlib.util.find_spec("pptx") is not None, "python-pptx（PPTX 导入）",
        "pip install python-pptx", required=False)
    so = SOFFICE if os.path.exists(SOFFICE) else shutil.which("soffice")
    row(so is not None, "LibreOffice（PPTX→PDF 兜底）",
        "只影响 PPTX；旁边放一份同名 PDF 就不需要", required=False)
    if so:
        print(f"      {so}")
    # ★ 新增：本地模型存在性（HF ID 会触发下载 → 被墙，这是最容易踩的坑）
    for tag, p in (("克隆模型", DEFAULTS["qwen_model"]), ("参考音频", DEFAULTS["ref_audio"])):
        exists = os.path.exists(p)
        row(exists, f"{tag}（本地）", p, required=False)
    print("必需依赖齐全" if ok else "缺少必需依赖")


def _local_read(p):
    """没有 docreader 时的本地兜底：docx 走 pandoc，pdf 走 pymupdf。读不了返回 None。"""
    suf = p.suffix.lower()
    try:
        if suf == ".docx" and shutil.which("pandoc"):
            return subprocess.run(["pandoc", str(p), "-t", "gfm", "--wrap=none"],
                                  capture_output=True, text=True, check=True).stdout
        if suf == ".pdf" and importlib.util.find_spec("fitz"):
            import fitz
            with fitz.open(p) as d:
                return "\n\n".join(pg.get_text() for pg in d)
    except Exception as e:
        print(f"  ⚠ 本地解析失败：{type(e).__name__}: {str(e)[:60]}")
    return None


def load_source(path):
    """读源文档。md/txt 直接读；docx/xlsx/pptx/pdf/epub 走文档解析服务（DOCREADER）。"""
    import urllib.parse
    p = Path(path)
    if not p.exists():
        sys.exit(f"源文档不存在：{path}")
    if p.suffix.lower() in (".md", ".markdown", ".txt"):
        text = p.read_text(encoding="utf-8")
    else:
        ext = p.suffix.lower().lstrip(".")
        res = None
        if DOCREADER:
            import urllib.parse
            url = (f"{DOCREADER}/read?file_name={urllib.parse.quote(p.name)}"
                   f"&file_type={urllib.parse.quote(ext)}")
            req = urllib.request.Request(url, data=p.read_bytes(),
                                         headers={"Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    res = json.load(r)
            except Exception as e:
                print(f"  ⚠ docreader 调用失败（{type(e).__name__}: {str(e)[:60]}），改用本地解析")
        else:
            print("  · 没配 DOCREADER，改用本地解析")
        if res and res.get("ok"):
            text = res.get("markdown") or ""
            print(f"  docreader: {len(text)} 字符 / {res.get('image_count', 0)} 张图 / "
                  f"{res.get('elapsed_ms', '?')} ms / 引擎 {res.get('metadata', {}).get('engine', '?')}")
        else:
            if res is not None:
                print(f"  ⚠ docreader 解析失败：{str(res)[:200]}")
            text = _local_read(p)
            if text is None:
                sys.exit(f"读不了 {p.name}：本地需要 " + ("pandoc（docx）" if ext == "docx" else "pymupdf（pdf）")
                         + "，或配 DOCREADER_URL 指向文档解析服务")
            print(f"  本地解析：{len(text)} 字符")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)        # 去掉图片引用
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_source(md, size=1500):
    sections, cur = [], []
    for line in md.splitlines():
        if re.match(r"#{1,4}\s", line) and cur:
            sections.append("\n".join(cur)); cur = []
        cur.append(line)
    if cur:
        sections.append("\n".join(cur))
    chunks = []
    for sec in sections:
        buf = ""
        for para in sec.split("\n\n"):
            if buf and len(buf) + len(para) > size:
                chunks.append(buf.strip()); buf = ""
            buf += para + "\n\n"
        if buf.strip():
            chunks.append(buf.strip())
    return chunks


def grams(s):
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def relevant(chunks, query, k=4, budget=6000):
    q = grams(query)
    score = lambda c: len(q & grams(c)) / (len(grams(c)) ** 0.5 + 1)
    ranked = sorted(range(len(chunks)), key=lambda i: -score(chunks[i]))
    picked, total = [], 0
    for i in ranked[:k]:
        if total + len(chunks[i]) <= budget:
            picked.append(i); total += len(chunks[i])
    return "\n\n---\n\n".join(chunks[i] for i in sorted(picked))   # 保持原文顺序


def llm(system, user, image=None):
    base = (os.environ.get("LLM_BASE_URL") or DEFAULT_LLM_BASE).rstrip("/")
    if not base:
        sys.exit(f"没配置 LLM 端点。设 LLM_BASE_URL 环境变量，或写进本地配置 {LOCAL_CONF}：\n"
                 f'  {{"llm_base": "http://<你的网关>:9000/v1", "llm_model": "main"}}')
    model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
    content = user
    if image:
        import base64
        b64 = base64.b64encode(Path(image).read_bytes()).decode()
        content = [{"type": "text", "text": user},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
    body = {"model": model, "temperature": 0.5,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'none')}"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        text = json.load(r)["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def review_narration_diff(tdir, pages):
    """review.md 与 narrations.json 哪些页不一致（build 以 review.md 为准）。"""
    nj = Path(tdir) / "narrations.json"
    if not nj.exists():
        return []
    try:
        d = json.loads(nj.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [n for n, x in pages if d.get(str(n), "").strip() != (x or "").strip()]


def update_review(rv, narr, pages):
    txt = rv.read_text(encoding="utf-8")
    for i in pages:
        txt = re.sub(rf"(^# {i} · .*?```\n)(.*?)(\n```)",
                     lambda m: m.group(1) + narr[str(i)] + m.group(3),
                     txt, count=1, flags=re.S | re.M)
    rv.write_text(txt, encoding="utf-8")


def parse_pages(s, n):
    out = set()
    for part in s.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return sorted(p for p in out if 1 <= p <= n)


def char_range(sec, cps, strict=False):
    """**唯一**的字数区间公式。strict=版本模式（要控时长，±10%）；
    否则按规范 §八 第 106 行：0.8–1.3×。别再各写一份。"""
    if strict:
        c = chars_for(sec, cps)
        return int(c * 0.9), int(c * 1.1)
    return int(sec * cps * 0.8), int(sec * cps * 1.3)


def grounding(out, tdir, s, brief=""):
    """数字溯源依据：本页画面 + 讲述要点 + 口径表 + 源文档。
    **不含上一页解说**——否则前一页编造的数字会一路传下去。"""
    parts = [s.get("title", ""), *s.get("text", []), *s.get("notes", [])]
    for f in (Path(tdir) / "brief.md", Path(out) / "source.md"):
        if Path(f).exists():
            parts.append(Path(f).read_text(encoding="utf-8"))
    if brief:
        parts.append(brief)
    return "\n".join(parts)


def slide_is_non_content(s, i, n):
    """这一页算不算「非内容页」（封面 / 目录 / 章节页 / 结尾页）。

    owner 2026-10-07：封面页与结尾页这类**没有正文内容**的页是例外——
    内容页的密度与字数下限规则不套在它们身上。命中任一条即算非内容页：
      1. 首页（封面）或末页（结尾）；
      2. 显式标了 fixed（按约定固定秒数的页就是封面/目录/结尾）；
      3. 标题里有 NON_CONTENT 的词（封面/目录/章节/结尾/封底/致谢）；
      4. `<section>` 的 class 命中非内容版式名（cover / toc / divider / section…）。"""
    if i in (1, n):
        return True
    if s.get("fixed"):
        return True
    blob = f"{s.get('title', '')} {s.get('cls', '')}"
    if any(k in blob for k in NON_CONTENT):
        return True
    return bool(set(re.split(r"\s+", (s.get("cls") or "").lower())) & NON_CONTENT_TPL)


def page_floor(sec, lo, cps=CPS, content=True):
    """【单版本模式】每页字数下限。

    内容页 = max(全局下限 lo, 规范 §八 的 0.8× 推算)；
    **非内容页（封面/目录/章节/结尾）= 只按自己的 sec 推算，不套全局下限**——
    否则 12 秒的封面页会被要求 100 字（≈22 秒语音），整片被拉长。"""
    if sec:
        spec = char_range(sec, cps)[0]
        return max(lo, spec) if content else spec
    return lo


# ─── 多版本（versions.json）：同一份幻灯片按不同时长出多版 ───
# 时长不直接交给模型（模型对"分钟"没有概念），一律换算成每页字数目标。

def chars_for(sec, cps):
    """每页秒数 → 字数。扣掉页首页尾留白，并把句间停顿算进去（约每 25 字一句）。
    与 cmd_build 的时长构成一致：LEAD + TAIL + Σ句子 + 句数×GAP。"""
    speech = max(sec - LEAD - TAIL, 3)
    return int(speech * cps / (1 + GAP * cps / 25))


def allocate(slides, total, skip=()):
    """把总时长按权重分到每页。fixed 页取固定值不参与缩放；权重用 sec，缺省 45。"""
    idx = [i for i in range(len(slides)) if i + 1 not in skip]
    fixed, w = {}, {}
    for i in idx:
        f = slides[i].get("fixed")
        try:
            f = float(f) if f not in (None, "") else None
        except (TypeError, ValueError):
            print(f"⚠ 第 {i+1} 页的 fixed 不是数字（{slides[i].get('fixed')!r}），按不固定处理")
            f = None
        if f:
            fixed[i] = f
        else:
            w[i] = float(slides[i].get("sec") or 45)
    free = max(total - sum(fixed.values()), 0.0)
    wsum = sum(w.values()) or 1.0
    secs = {i + 1: (fixed[i] if i in fixed else free * w[i] / wsum) for i in idx}
    short = sorted(p for p, s in secs.items() if s < MIN_SEC)
    if short:
        print(f"⚠ 第 {short} 页只分到不足 {MIN_SEC} 秒，建议在 versions.json 里 skip 部分页面")
    secs = {p: max(MIN_SEC, s) for p, s in secs.items()}
    got = sum(secs.values())        # 钳制后总长会超标 → 必须报出来，不能静默
    if got > total + 0.5:
        print(f"⚠ 因每页下限 {MIN_SEC} 秒，实际总长 {got / 60:.2f} 分钟"
              f"（比目标多 {got - total:.0f} 秒）> 目标 {total / 60:.2f} 分钟")
    if not all(slides[i].get("sec") or slides[i].get("fixed") for i in idx):
        print("  （有页面没设权重，按默认 45 平均分配；要差别对待就在 data-sec / slides.json 里设）")
    return secs

def page_seconds(slides, total_sec, skip=()):
    """版本模式下每页的目标秒数。

    规范 §六 第 84 行：deck 的 `data-sec` 是**绝对秒**，不是权重。所以按声明秒数
    **等比缩放**到目标总时长（封面/目录这类 fixed 页保持原值），不再把 data-sec 当权重
    重新分配。没有声明秒数的页（旧 slides.json / json 大纲）才回退到按权重分配。
    这样"同一份 deck 出 12/14 分钟两版"改的是**解说词详略**，不动页数。
    """
    keep = [s for i, s in enumerate(slides, 1) if i not in skip]
    if keep and all(s.get("sec") for s in keep):
        fixed = sum(float(s["sec"]) for s in keep if s.get("fixed"))
        decl = sum(float(s["sec"]) for s in keep if not s.get("fixed"))
        room = max(0.0, total_sec - fixed)
        scale = room / decl if decl else 0.0
        out = {}
        for i, s in enumerate(slides, 1):
            if i in skip:
                continue
            out[i] = float(s["sec"]) if s.get("fixed") else float(s["sec"]) * scale
        return out
    return allocate(slides, total_sec, skip)



def tts_fingerprint(cfg):
    """语速与音色绑定：音色、后端、变速任一变化，cps 校准即作废。
    读法规则也算在内——实测修了年份读法后同一句话的音频变短，cps 从 4.43 跳到 5.16，
    旧的校准值若被沿用，之后所有版本的时长都会偏短。"""
    if cfg.get("backend") == "qwen":
        voice = cfg.get("ref_audio") or f"speaker:{cfg.get('speaker')}"
    else:
        voice = f"edge:{cfg.get('edge_voice')}:{cfg.get('edge_rate')}"
    return {"backend": cfg.get("backend"), "voice": voice,
            "speed": float(cfg.get("speed") or 1.0),
            "speak": hashlib.sha1("".join(p.pattern for p, _ in _SPEAK_RULES).encode()).hexdigest()[:8],
              "pron": pron_hash()}   # 读音表也要算进来


def load_cps(out, cfg=None):
    """读实测语速。给了 cfg 就校验指纹，不匹配视为无效（只改音色/语速不重校准 → 全长跑偏）。"""
    f = Path(out, "cps.json")
    default = float(os.environ.get("CPS", "4.5"))
    if not f.exists():
        return default
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return default
    if cfg is not None and d.get("fingerprint") and d["fingerprint"] != tts_fingerprint(cfg):
        print("⚠ cps.json 的音色/后端/语速与当前配置不一致，已作废，按默认估算；建议重做一次母版校准")
        return default
    return float(d.get("cps") or default)


def load_version(deck, out, name):
    """读 versions.json 里的一条版本配置。--version 不传时返回 None（走老的单版本流程）。"""
    vf = Path(deck).parent / "versions.json"
    if not vf.exists():
        sys.exit(f"指定了 --version {name}，但 {vf} 不存在")
    try:
        all_v = json.loads(vf.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"versions.json 解析失败：{e}")
    v = all_v.get(name)
    if v is None:
        sys.exit(f"versions.json 里没有版本 {name!r}；现有：{', '.join(all_v) or '（空）'}")
    v = dict(v); v["name"] = name
    v["dir"] = Path(out, "versions", name); v["dir"].mkdir(parents=True, exist_ok=True)
    v["skip"] = set(v.get("skip", []))
    v["brief_text"] = ""
    if v.get("brief"):
        bf = Path(deck).parent / v["brief"]
        if not bf.exists():
            sys.exit(f"版本 {name} 的 brief 文件不存在：{bf}")
        v["brief_text"] = bf.read_text(encoding="utf-8").strip()
    if not v.get("minutes"):
        sys.exit(f"版本 {name} 必须写 minutes")
    return v


def ungrounded_numbers(text, grounded):
    """text 里的数字若不在 grounded（画面+讲述要点+源文档段落+上一页解说）中，视为可疑（防编造数字）。
    逗号归一化（1,000 与 1000 视为同一数，避免误报）；跳过单位数整数（常为范围/序数，噪声大）。"""
    nums = re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
    g = set(re.findall(r"\d+(?:\.\d+)?", grounded.replace(",", "")))
    return sorted({n for n in nums if (len(n) > 1 or "." in n) and n not in g})


def lint(text, lo, grounded=None, disc=False):
    """检查字数下限、问句、套话、画面指代、俚语、绝对化、句子过碎、禁用符号、免责语、数字溯源。
    返回问题列表（空 = 合格）。只设字数下限不设上限：信息讲透优先。"""
    issues = []
    n = len(re.sub(r"\s", "", text))
    if n < lo:
        issues.append(f"字数偏少（{n} < {lo}）")
    if re.search(r"[？?]", text):
        issues.append("出现问句")
    hit = [w for cat, words in BANNED.items() for w in words if w in text]
    for s in sentences(text):                       # 首先/其次/最后 只查句首
        if s.lstrip("“\"").startswith(tuple(OPENERS)):
            hit.append(s[:2])
    if hit:
        issues.append("禁用套话：" + "、".join(dict.fromkeys(hit)))
    sents = sentences(text)
    if sents and sum(len(s) for s in sents) / len(sents) < 14:
        issues.append("句子过碎，像念提纲")
    bad = [c for c in "（）()→—/＃#*[]｜|" if c in text]
    if bad:
        issues.append("出现禁用符号：" + "、".join(sorted(set(bad))))
    if not DISCLAIMER_ON:                      # 默认：任何页都不许出现免责/声明类内容
        if re.search(DISC_WORDS, text):
            issues.append("出现免责/声明类内容（本流程默认不加，要加请用 --disclaimer）")
    elif disc:
        if not re.search(DISC_WORDS, text):
            issues.append("缺少免责声明")
    elif re.search(DISC_WORDS, text):
        issues.append("免责语只出现在指定页")
    if grounded is not None:
        un = ungrounded_numbers(text, grounded)
        if un:
            issues.append("数字无出处：" + "、".join(un))
    return issues


def cmd_narrate(out, slides, v=None, source=None, pages=None,
                lo=100, hi=180, brief=None, vision=False):
    """逐页生成解说词。每页独立调用 LLM（system 带本页的免责口径：默认关闭、禁止出现），不再整篇一次生成——
    整篇模式会让模型套用"承接→结论→引出"的固定结构，写出电报体碎句。

    v 不为 None 时走版本模式：总时长按权重分到每页，再换算成每页字数目标；
    v["from"] 有值时从母版的解说词**压缩**，而不是重新创作。"""
    tdir = v["dir"] if v else Path(out)
    _rv0 = Path(tdir) / "review.md"
    if _rv0.exists():                      # 生效的 --length 落盘，review-doc 与审核台据此显示同一套区间
        set_review_cfg(_rv0, length=f"{lo}-{hi}")
    load_pron(out, Path(out).parent)   # 读音表进 cps 指纹：这里不加载会误判校准失效
    n = len(slides)
    write_outline(tdir, slides)
    nj, rv = tdir / "narrations.json", tdir / "review.md"
    narr = json.loads(nj.read_text(encoding="utf-8")) if nj.exists() else {}
    src = load_source(source) if source else ""
    if src:
        Path(out, "source.md").write_text(src, encoding="utf-8")   # 源文档各版本共用
    # brief 优先级：命令行 --brief > versions.json 里的 brief
    brief = (Path(brief).read_text(encoding="utf-8").strip() if brief
             else (v["brief_text"] if v else ""))
    if brief:
        (tdir / "brief.md").write_text(brief, encoding="utf-8")
    save = lambda: nj.write_text(json.dumps(dict(sorted(narr.items(), key=lambda kv: int(kv[0]))),
                                            ensure_ascii=False, indent=2), encoding="utf-8")
    chunks = chunk_source(src) if src else []
    title = lambda t: t["title"] or (t["text"][0][:20] if t["text"] else "")

    secs, cps, master = None, None, None
    if v:
        cps = load_cps(out, parse_review(rv)[0] if rv.exists() else None)
        secs = page_seconds(slides, v["minutes"] * 60, v["skip"])
        if v.get("from"):
            mf = Path(out, "versions", v["from"], "narrations.json")
            if not mf.exists():
                sys.exit(f"先生成母版 {v['from']}（{mf} 不存在）")
            master = json.loads(mf.read_text(encoding="utf-8"))
            if nj.exists() and mf.stat().st_mtime > nj.stat().st_mtime:
                print(f"⚠ 母版 {v['from']} 比本版解说词新，本版可能已过期，建议重跑本版")
        print(f"版本 {v['name']}：目标 {v['minutes']} 分钟 · {len(secs)} 页 · 语速 {cps} 字/秒"
              + (f" · 从母版 {v['from']} 压缩" if master else ""))
    targets = pages or (sorted(secs) if v else range(1, n + 1))
    print(f"逐页模式：源文档切成 {len(chunks)} 块，生成 {len(targets)} 页…")
    total_chars = 0
    for i in targets:
        pg = slides[i - 1]
        page = "\n".join([pg["title"], *pg["text"]]).strip()
        notes = "\n".join(pg["notes"]) if pg.get("notes") else "（无）"
        src_seg = relevant(chunks, page) if chunks else ""
        prev = narr.get(str(i - 1), "")
        disc = pg.get("disc", False)
        grounded = grounding(out, tdir, pg, brief) + "\n" + src_seg   # 不含上一页解说
        if v:
            c = chars_for(secs[i], cps)
            lo_i, hi_i = char_range(secs[i], cps, strict=True)
            total_chars += c
        else:
            _content = not slide_is_non_content(pg, i, n)
            lo_i, hi_i = page_floor(pg["sec"], lo, cps or CPS, content=_content), None
        system = NARRATE_SYSTEM.format(
            disclaimer=((DISCLAIMER if disc else "本页不涉及财务预测，不要说免责语。")
                        if DISCLAIMER_ON else NO_DISC_RULE),
            brief=brief or "（无场景卡）")
        toc = "\n".join(f"{j}. {'**' + title(t) + '**（本页）' if j == i else title(t)}"
                        for j, t in enumerate(slides, 1))
        user = (f"【整套幻灯片目录】\n{toc}\n\n" +
                f"【上一页的解说】\n{prev or ('（这是第一页）' if i == 1 else '（无）')}\n\n" +
                f"【当前页·画面】\n{page}\n\n" +
                f"【当前页·讲述要点】\n{notes}\n\n" +
                f"【源文档相关段落】\n{src_seg or '（无）'}")
        if master and master.get(str(i)):
            grounded += "\n" + master[str(i)]          # 母版文本也算依据，防压缩时改数字
            user = (f"【任务】把【原稿】压缩为 {lo_i}–{hi_i} 字，用于更短的视频版本。"
                    "保留核心判断和最重要的依据，按【讲述要点】的顺序取舍。"
                    "不新增原稿中没有的事实，保留的数字一字不改。"
                    "重新组织成连贯的段落，不要逐句删减。\n\n"
                    f"【原稿】\n{master[str(i)]}\n\n"
                    f"【当前页：第 {i}/{n} 页】\n标题：{pg['title']}\n"
                    f"讲述要点：{notes}\n\n"
                    f"【字数】{lo_i}–{hi_i} 字")
        elif v:
            user += (f"\n\n【字数】{lo_i}–{hi_i} 字。字数有限时按讲述要点的顺序取舍，"
                     "优先讲前面的；宁可少讲一条，也不要每条都一笔带过。")
        img = Path(out, "slides", f"{i:03d}.png") if vision else None
        if img is not None and not img.exists():
            print(f"    ⚠ 第 {i} 页找不到截图 {img.name}，本页退回文字模式")
            img = None
        if img is not None:
            user += "\n\n【当前页截图】已附图片，可参考图中的图表和数据，但解说中不要提及画面。"
        text = llm(system, user, img).strip().strip('"“”')
        for attempt in range(2):
            iss = lint(text, lo_i, grounded, disc=disc)
            if v:                       # 版本模式才管上限，且放宽到 OVER_TOL 倍（内容优先）
                got_n = len(re.sub(r"\s", "", text))
                if got_n > hi_i * OVER_TOL:
                    iss.append(f"字数超出上限（{got_n} > {hi_i}）")
            if vision:
                # 看图模式：模型可能从图片里读出数字，而脚本核验不了图片 → 只提醒，不退回重写
                num = [x for x in iss if x.startswith("数字无出处")]
                if num:
                    print(f"    ⚠ 第 {i} 页数字可能来自图片，请人工核对：{num[0].split('：', 1)[-1]}")
                iss = [x for x in iss if not x.startswith("数字无出处")]
            if not iss:
                break
            print(f"    ⚠ 第 {i} 页 {iss} → 重写 {attempt + 1}/2")
            rewrite = llm(system, f"【当前页·画面】\n{page}\n\n【当前页·讲述要点】\n{notes}\n\n"
                                   f"【本页解说·草稿】\n{text}\n\n【问题】\n" + "\n".join(iss) +
                                   "\n\n请重写这一页解说词，只输出正文。", img).strip().strip('"“”')
            if rewrite:
                text = rewrite
        narr[str(i)] = text
        save()                                                    # 每页都存，中断不丢进度
        print(f"  ✔ 第 {i:2d} 页  {len(text)} 字"
              + (f"（目标 {lo_i}–{hi_i}）" if v else f"（下限 {lo_i}）"))
        if iss:
            print(f"    ⚠ 第 {i} 页重写 2 次后仍有问题：{iss}（已存进 narrations.json，可在审核台改）")

    miss = [i for i in range(1, n + 1) if i in (sorted(secs) if v else range(1, n + 1))
            and not narr.get(str(i))]
    print("已写入 narrations.json" + (f"，缺少第 {miss} 页" if miss else ""))
    if v:
        print(f"版本 {v['name']}：计划总字数约 {total_chars}，目标 {v['minutes']} 分钟")
    if rv.exists():
        if pages:
            update_review(rv, narr, pages)
            print(f"已同步 review.md 中第 {list(pages)} 页，其他页保持不变")
        else:
            # 全量重生成：把 review.md 的解说词同步为新 narrations.json（保留原配置头），
            # 避免 build 读到旧解说词（build 以 review.md 为准）。
            cfg, _ = parse_review(rv)
            write_review(rv, slides, narr, cfg, skip=(v["skip"] if v else ()),
                         slides_dir=Path(out, "slides"))
            print("已同步 review.md 为新解说词（保留原配置），可直接 build")


def cmd_review(deck, out, slides, reset, start, v=None):
    tdir = v["dir"] if v else Path(out)          # 解说词/审稿文档随版本走，画面各版本共用
    write_outline(tdir, slides)
    is_html = Path(deck).suffix.lower() in (".html", ".htm")
    if is_html:                                        # 导入的 PPTX/PDF 已在 import 时截过图
        render(deck, out, len(slides), start)
    rv = tdir / "review.md"
    if rv.exists() and not reset:
        print(("已重新截图，" if is_html else "已更新 outline.md，") + "保留现有 review.md（加 --reset 可重新生成）")
        return
    nj = tdir / "narrations.json"
    narr = json.loads(nj.read_text(encoding="utf-8")) if nj.exists() else {}
    # 保住手改过的配置：--reset 只重建正文，不能把 review.md 里的
    # ref_audio / ref_text / speed / backend 冲掉（否则 build 会悄悄换成预置音色）
    cfg = dict(DEFAULTS)
    if rv.exists():
        try:
            cfg = parse_review(rv)[0]
            print("  已保留 review.md 里改过的配置")
        except Exception as e:
            print(f"  ⚠ 读不出旧 review.md 的配置（{type(e).__name__}），用默认值")
    write_review(rv, slides, narr, cfg, skip=(v["skip"] if v else ()),
                 slides_dir=Path(out, "slides"))
    print(f"已生成 {rv}，修改确认后运行 build")


def mux(out, cfg, total, fontdir=None, name="final-video.mp4"):
    mode = cfg.get("subtitles", "burn")
    if mode == "burn" and not has_libass():
        print("⚠ 当前 ffmpeg 不支持 subtitles 滤镜，改为软字幕")
        mode = "soft"
    # frame=letterbox：画面压到 960 高、贴顶，底部留 120px 黑边给字幕（导入的旧 PPT 用）
    box = 960 if cfg.get("frame") == "letterbox" else 1080
    y = "0" if box < 1080 else "(oh-ih)/2"
    vf = (f"fps=30,scale=1920:{box}:force_original_aspect_ratio=decrease,"
          f"pad=1920:1080:(ow-iw)/2:{y}:color={cfg.get('pad_color', 'black')},format=yuv420p")
    if mode == "burn":
        margin = 8 if box < 1080 else 24
        style = f"FontName=Noto Sans SC,FontSize=15,Outline=1.2,Shadow=0,MarginV={margin}"
        fontsdir = f":fontsdir={fontdir}" if fontdir else ""
        vf += f",subtitles=subs.srt{fontsdir}:force_style='{style}'"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-stats",
           "-f", "concat", "-safe", "0", "-i", "list.ffconcat", "-i", "narration.wav"]
    if mode == "soft":
        cmd += ["-i", "subs.srt"]
    cmd += ["-map", "0:v", "-map", "1:a"]
    if mode == "soft":
        cmd += ["-map", "2:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
    # -t：硬截断到音频总时长。ffconcat 最后一行重复的 file 没有 duration，
    #     ffmpeg 会补一次默认时长 → 最后一页被播两遍（实测 24.67s 的日志出 30.57s 的视频）。
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-t", f"{total:.3f}", name]
    subprocess.run(cmd, cwd=out, check=True)
    print(f"\n完成：{Path(out, name)}（{total / 60:.1f} 分钟）")


# ─── 审核状态（gates.json）：文件一改就自动变成"需重审" ─────────────
# 记录"哪一次审核通过时，这些文件是什么哈希"。build 前比对：变过就拒绝，
# 除非显式 --force。这样 SKILL.md 里"deck 一改必须重跑 review"的提醒由代码保证。

def out_dir(deck):
    """输出目录：HTML → video-output/；PPTX/PDF → <文件名>-video/。
    所有命令都用这一个，别再各写一份（否则路线 B 的审核台/预览找不到文件）。"""
    d = Path(deck).resolve()
    return d.parent / ("video-output" if d.suffix.lower() in (".html", ".htm") else f"{d.stem}-video")


def gate_files(deck, out, tdir):
    """出片前要比对的文件。review.md 是 build 的真源，必须在清单里。"""
    f = {"deck": Path(deck), "narrations": Path(tdir) / "narrations.json",
         "review": Path(tdir) / "review.md"}
    if Path(out, "slides.json").exists():
        f["slides"] = Path(out, "slides.json")       # 路线 B 的 sec / notes 改在这里
    omd = find_outline_md(deck)
    if omd:
        f["大纲"] = omd
    return f


def file_sha(p):
    p = Path(p)
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.exists() else None


def gates_path(out):
    return Path(out) / "gates.json"


def gates_load(out):
    try:
        return json.loads(gates_path(out).read_text(encoding="utf-8"))
    except Exception:
        return {}


def gates_approve(out, gate, files):
    """把某道门的通过状态与文件哈希写进 gates.json。"""
    Path(out).mkdir(parents=True, exist_ok=True)
    d = gates_load(out)
    d.setdefault("gates", {})[gate] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": {k: {"path": str(v), "sha": file_sha(v)} for k, v in files.items() if v},
    }
    gates_path(out).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def gates_stale(out, gate, files):
    """哪些文件在该门通过之后被改过（或从没通过过该门）。"""
    rec = gates_load(out).get("gates", {}).get(gate)
    if not rec:
        return None                                  # 从没审过
    bad = []
    for name, path in files.items():
        old = (rec.get("files", {}).get(name) or {}).get("sha")
        if old is None or file_sha(path) != old:   # 审核时不存在 = 没审过
            bad.append(name)
    return bad


def find_outline_md(near):
    """在 deck 附近按约定找 大纲.md（deck 同目录、上一级、上两级）。"""
    if not near:
        return None
    for d in (Path(near), Path(near).parent, Path(near).parent.parent):
        c = d / OUTLINE_MD
        if c.exists():
            return c
    return None


def cmd_gates(out, deck=None):
    """打印审核状态：哪道门什么时候通过、之后哪些文件被改过。"""
    d = gates_load(out)
    gates = d.get("gates", {})
    if not gates:
        print("还没有审核记录（gates.json 为空）")
        return 0
    bad_total = 0
    for name, rec in gates.items():
        stale = gates_stale(out, name, {k: v["path"] for k, v in rec.get("files", {}).items()})
        bad_total += len(stale or [])
        flag = "✔ 有效" if not stale else f"⚠ 需重审：{'、'.join(stale)}"
        print(f"  [{name}] {rec.get('at', '?')}  {flag}")
        for k, v in rec.get("files", {}).items():
            print(f"      {k}: {v['path']}")
    return 1 if bad_total else 0


def cmd_build(out, slides, v=None, deck=None, force=False):
    tdir = v["dir"] if v else Path(out)          # 解说词/中间件/成片随版本走；画面和缓存共用

    # —— 门禁（Gate 2）：文件在通过审核之后被改过、或从没审过 → 拒绝出片 ——
    _files = gate_files(deck, out, tdir) if deck else {"narrations": tdir / "narrations.json"}
    _stale = gates_stale(out, "narration", _files)
    if _stale is None:
        _stale = ["（还没通过 Gate 2）"]
    if _stale and not force:
        print("✘ 不能出片：" + "、".join(_stale)
              + "。先在审核台过 Gate 2；确知没影响才用 --force（agent 不得自行使用）")
        return 2
    rv = tdir / "review.md"
    if not rv.exists():
        sys.exit("先运行 review")
    _rf = tdir / "redo.json"
    redo = set()
    if _rf.exists():
        try:
            redo = {int(k) for k in json.loads(_rf.read_text(encoding="utf-8"))}
        except Exception:
            redo = set()
        if redo:
            print(f"重录标记：第 {sorted(redo)} 页 —— 会清掉这些页的句子缓存再合成")
    cfg, pages = parse_review(rv)
    _mm = review_narration_diff(tdir, pages)
    if _mm:
        print(f"⚠ 第 {_mm} 页 review.md 与 narrations.json 不一致，**以 review.md 为准**"
              "（要同步：narrate --pages，或在审核台把该页重存一次）")
    pages = [(n, t) for n, skip, t in pages if not skip]
    bad = [n for n, t in pages if not t or t == PLACEHOLDER]
    if bad:
        sys.exit(f"这些页还没有解说词：{bad}")
    if not pages:
        sys.exit("所有页面都被跳过了")
    load_pron(out, deck)          # 读音表：输出目录 > deck 同目录 > 环境变量
    tts = QwenTTS(cfg) if cfg["backend"] == "qwen" else EdgeTTS(cfg)
    print(f"配音模式: {'克隆 ' + tts.ref_audio if getattr(tts, 'clone', False) else '预置音色 ' + cfg['speaker']}")
    cache = Path(out, "tts-cache"); cache.mkdir(exist_ok=True)   # 各版本共用：同句只合成一次
    secs = page_seconds(slides, v["minutes"] * 60, v["skip"]) if v else None
    if v:
        print(f"版本 {v['name']}：目标 {v['minutes']} 分钟 · {len(secs)} 页")
    silence = lambda s: np.zeros(int(round(s * SR)), dtype=np.int16)

    track, cues, ffc, t = [], [], ["ffconcat version 1.0"], 0.0
    speech, chars = 0.0, 0                                          # 实测语速累计
    for num, text in pages:
        parts, cur = [silence(LEAD)], LEAD
        seg_list = sentences(text)
        spoken = [speak(s) for s in seg_list]          # TTS 读口语；字幕仍用书面 s
        if num in redo:                                  # 重录页：删掉句子缓存（含变速副本）
            for _sp in spoken:
                for _f in cache.glob(cache_key(tts, _sp) + "*.wav"):
                    _f.unlink()
        for s, wavp in zip(seg_list, cached_many(tts, spoken, cache)):
            a = read_wav_sped(wavp, float(cfg.get("speed", 1.0) or 1.0), cache)
            d = len(a) / SR
            cues += split_cue(s, t + cur, t + cur + d)
            parts += [a, silence(GAP)]
            cur += d + GAP
            speech += d
            chars += len(re.sub(r"\W", "", s))
        parts.append(silence(TAIL))
        seg = np.concatenate(parts)
        dur = len(seg) / SR
        track.append(seg)
        # 图片必须绝对路径：concat 文件在版本目录里，相对路径会解析到 versions/<名>/slides/
        ffc += [f"file '{Path(out, 'slides', f'{num:03d}.png').resolve()}'", f"duration {dur:.6f}"]
        t += dur
        tgt = secs.get(num) if secs else slides[num - 1].get("sec")
        # 内容优先：比目标短（内容被砍）才告警；比目标长是信息量充足，正常。
        warn = tgt and dur / float(tgt) < 0.8
        print(f"  ✔ 第 {num:2d} 页  {dur:5.1f}s"
              + (f" / 目标 {float(tgt):.0f}s" if tgt else "")
              + ("  ⚠ 偏短，可能漏内容" if warn else ""))
    ffc.append(f"file '{Path(out, 'slides', f'{pages[-1][0]:03d}.png').resolve()}'")   # 末帧重复一次
    if speech:
        cps = chars / speech
        print(f"实测语速 {cps:.2f} 字/秒")
        Path(out, "cps.json").write_text(
            json.dumps({"cps": round(cps, 2), "fingerprint": tts_fingerprint(cfg)},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("已存入 cps.json（绑定后端/音色/语速；换音色或改 speed 会自动作废）")
    if v:
        tgt_total = v["minutes"] * 60
        print(f"实际 {t / 60:.1f} 分钟 / 目标 {v['minutes']} 分钟（偏差 {t / tgt_total - 1:+.0%}）")

    write_wav(tdir / "narration.wav", np.concatenate(track))
    (tdir / "list.ffconcat").write_text("\n".join(ffc) + "\n", encoding="utf-8")
    (tdir / "subs.srt").write_text(
        "".join(f"{i}\n{ts(a)} --> {ts(b)}\n{c}\n\n" for i, (a, b, c) in enumerate(cues, 1)),
        encoding="utf-8")
    fontdir = Path(out).parent / "subtitles" / "fonts"
    if redo and _rf.exists():                      # 用过即归档，避免下次又整页重录
        _rf.rename(tdir / f"redo-{time.strftime('%Y%m%d-%H%M%S')}.json")
        print("重录标记已归档")
    mux(tdir, cfg, t, str(fontdir) if fontdir.is_dir() else None,
        f"final-{v['name']}.mp4" if v else "final-video.mp4")


# ─── 审核点工作文件（三个 gate 的人审产物，全部确定性、不调 LLM）──────
# Gate 1 大纲 → 大纲审定稿（含时长/字数预算表）
# Gate 2 画面 + 解说词 → 预览 PDF + 解说词审定稿
# Gate 3 成片 → 质检报告（时长/逐页偏差/字幕/音量/重录表）

# ─── 大纲 → deck 骨架（依据规范 §六 第 84 行）──────────────────
# 规范原文：「每页的讲述要点原样写入 <div class="notes">，时长写入
# <section class="slide" data-sec="45">」。这一环我原来完全没有——
# 它是"依据大纲.md 去做 slides"的接口。（思路吸收自 reviewer 的 outline_md.py）

def notes_html(page):
    """讲述要点 → <div class="notes">（出处标注保留，它是解说的溯源依据）。"""
    items = [x.strip() for x in page["notes"]]
    return '<div class="notes">\n' + "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)) + "\n</div>"


def deck_section(page):
    """一页大纲 → 一个 <section class="slide"> 骨架。时长用绝对秒（规范 §六）。"""
    sec = f'{page["sec"]:g}' if page["sec"] else "45"
    body = [f'<section class="slide" data-title="{_esc(page["title"], quote=True)}" data-sec="{sec}">',
            f'  <!-- 版式（规范 §一 第 4 步，按它选组件）：{page["layout"] or "（待定）"} -->']
    if page["info"]:
        body.append("  <!-- 信息点（画面上要放的结论与证据，不含来源标注）：")
        body += [f"       {i}. {x}" for i, x in enumerate(page["info"], 1)]
        body.append("  -->")
    body += ["  <!-- TODO: 按上面版式填组件；画面放结论和证据，解释留给 .notes -->",
             "  " + notes_html(page).replace("\n", "\n  ").rstrip(),
             "</section>"]
    return "\n".join(body)


def find_gates_dir(near):
    """在产物附近找 gates.json（deck 旁边、或 video-output/ 里）。"""
    for d in (Path(near), Path(near) / "video-output", Path(near).parent, Path(near).parent / "video-output"):
        if (d / "gates.json").exists():
            return d
    return None


def gate_guard(near, gate, files, label, force=False):
    """审核点守卫：没有该审核点的记录（或产物在通过之后被改过）→ 拒绝进入下一步。

    owner 2026-10-07 定的规则：**任何审核点都必须等 owner 完成审核才能进下一步**；
    agent 自己的检查不算审核通过，也不得用 --force 绕过（除非 owner 明确要求）。
    返回 True = 可以继续。"""
    gd = find_gates_dir(near)
    stale = gates_stale(gd, gate, files) if gd else None
    if stale is None:
        stale = [f"（从没见过 {label} 的审核记录）"]
    if stale and not force:
        print(f"✘ {label} 还没通过：{'、'.join(stale)}")
        print("  规则（owner 2026-10-07）：任何审核点必须等 owner 完成审核才能进下一步。")
        print("  agent 不得自行判定通过（自检/测试全绿都不算），也不得用 --force 绕过。")
        print("  请先在审核台让 owner 点「完成」，再重跑本命令。")
        return False
    if force and stale:
        print(f"⚠ 已用 --force 越过 {label}：{'、'.join(stale)}")
    return True


def cmd_deck_skeleton(md_path, out_path=None, force=False):
    """把 大纲.md 落成 deck 骨架（每页一个 <section>，含 data-title / data-sec / .notes）。"""
    p = Path(md_path)
    if not p.exists():
        sys.exit(f"没有 {p}")
    # 门槛：大纲这一关（Gate ①）没过，不许开始做 slides
    if not gate_guard(p.parent, "outline", {"大纲": p}, "Gate ①（大纲）", force):
        return 2
    doc = parse_outline_md(p.read_text(encoding="utf-8"))
    if not doc["pages"]:
        sys.exit("没解析到页：页标题要写成『## P5 结论式标题』")
    parts = [deck_section(x) for x in doc["pages"]]
    dest = Path(out_path) if out_path else p.with_name("deck-骨架.html")
    dest.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    print(f"已写入 {dest}：{len(doc['pages'])} 个 <section>"
          f" · data-sec 合计 {sum(x['sec'] or 45 for x in doc['pages']):.0f} 秒"
          f" · notes 共 {sum(len(x['notes']) for x in doc['pages'])} 条")
    return 0


def cmd_outline(path, out_path=None):
    """Gate 1：大纲 → 审核报告 + 预检。

    格式与密度规则**以 owner 的规范为准**（business-deck-spec.md §一 方法论/大纲格式、
    §二 信息密度）。规范没有要求这套自动检查——`outline_preflight` 是我加的便利校验，
    ERROR 必须改，WARN 请人确认。`.md` 是主路径；`.json` 仅保留给旧产物。
    """
    src = Path(path)
    if not src.exists():
        sys.exit(f"没有 {src}")

    if src.suffix.lower() in (".md", ".markdown"):
        parsed = parse_outline_md(src.read_text(encoding="utf-8"))
        src_text = None
        m = re.search(r"^-\s*素材\s*[：:]\s*(.+)$", parsed["preamble"], re.M)
        if m:
            cand = src.parent / m.group(1).strip().strip("`")
            if cand.exists():
                src_text = cand.read_text(encoding="utf-8", errors="ignore")
        issues = outline_preflight(parsed, src_text)
        budget, cps, minutes = outline_md_budget(parsed)
        by = {}
        for i in issues:
            by.setdefault(i["page"], []).append(i)
        nerr = sum(i["level"] == "ERROR" for i in issues)
        nwarn = sum(i["level"] == "WARN" for i in issues)
        total = sum(p["sec"] or 0 for p in parsed["pages"])
        L = ["# Gate 1 · 大纲审核报告", "",
             f"- 大纲：`{src.name}`",
             "- 依据：`business-deck-spec.md` §一（方法论 / 大纲格式）、§二（信息密度）",
             f"- 解说词**规划**时长：各页合计 **{total:.0f} 秒**"
             + (f" · 大纲标的目标 {minutes:g} 分钟（解说词详略自定，成片长度是结果，不是 PPT 的约束）"
                if minutes else ""),
             f"- 字数公式 `sec × {cps}`，区间 0.8–1.3×（规范 §八 第 104/106 行）",
             f"- 共 **{len(parsed['pages'])}** 页 · **ERROR {nerr}** · WARN {nwarn}", ""]
        if by.get(None):
            L += ["## 全局问题", ""] + [f"- **{x['level']}** {x['msg']}" for x in by[None]] + [""]
        L += ["## 总览", "", "| 页 | 标题 | 版式 | 信息点 | 要点 | 秒 | 目标字数 | 问题 |",
              "|---|---|---|---|---|---|---|---|"]
        for pg, b in zip(parsed["pages"], budget):
            ps = by.get(pg["n"], [])
            flag = "❌" if any(x["level"] == "ERROR" for x in ps) else ("⚠" if ps else "✓")
            title = pg["title"] + ("（非内容页，信息点不限）" if any(k in pg["title"] for k in NON_CONTENT) else "")
            L.append(f"| P{pg['n']} | {title} | {pg['layout'][:26] or '—'} | "
                     f"{pg['info_declared'] if pg['info_declared'] is not None else len(pg['info'])} | "
                     f"{len(pg['notes'])} | {b['sec']:.0f} | {b['lo']}–{b['hi']} | {flag} |")
        L += [""]
        pages_with = [pg for pg in parsed["pages"] if by.get(pg["n"])]
        if pages_with:
            L += ["## 逐页问题", ""]
            for pg in pages_with:
                L.append(f"### P{pg['n']} · {pg['title']}")
                L += [f"- **{x['level']}** {x['msg']}" for x in by[pg["n"]]]
                L.append("")
        L += ["## 人工审核要点（脚本查不了）", "",
              "- 主线：各部分连起来，能否支撑开头那句主线",
              "- 标题：每页标题单独读，是不是一个明确的判断（规范 §一 第 14 行）",
              "- 讲述要点：是不是画面的**补充**（原因/条件/含义），有没有复述画面文字（规范 §一 第 33 行）",
              "- 信息点：6–10 是否合理，有没有该拆页的（规范 §二 第 41 行）",
              "- 时长：内容页 40–60 秒是否合理（规范 §一 第 11 行）", ""]
        dest = Path(out_path) if out_path else src.parent / "大纲-审核报告.md"
        dest.write_text("\n".join(L), encoding="utf-8")
        print(f"大纲 {len(parsed['pages'])} 页 · 合计 {total:.0f} 秒 · ERROR {nerr} · WARN {nwarn}")
        for i in [x for x in issues if x["level"] == "ERROR"][:12]:
            print(f"  ✘ {'P%s ' % i['page'] if i['page'] else ''}{i['msg']}")
        print(f"报告：{dest}")
        return 1 if nerr else 0

    # —— 旧路径：outline.json（仅兼容旧产物）——
    try:
        o = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"outline.json 解析失败：{e}")
    pages = o.get("pages") or []
    if not pages:
        sys.exit("outline.json 里没有 pages")
    secs = allocate([{"title": p.get("title", ""), "sec": p.get("weight"), "fixed": p.get("fixed"),
                      "disc": bool(p.get("disc")), "text": p.get("points", []),
                      "notes": p.get("notes", [])} for p in pages],
                    float(o.get("minutes") or 14) * 60)
    cps = float(os.environ.get("CPS", "4.5"))
    L = [f"# {o.get('project', '（未命名）')} · 大纲审定稿（旧 json 格式，建议改用 大纲.md）", ""]
    for i, p in enumerate(pages, 1):
        c = chars_for(secs[i], cps)
        L += [f"## P{i} {p.get('title', '')}", "",
              f"- 版式：{p.get('layout', '')}", f"- 时长：{secs[i]:.0f} 秒",
              f"- 目标字数：{char_range(secs[i], cps)[0]}–{char_range(secs[i], cps)[1]}", ""]
        if p.get("support"):
            L += ["- 支撑材料："] + [f"  - {x}" for x in p["support"]] + [""]
        if p.get("notes"):
            L += ["- 讲述要点："] + [f"  {k}. {x}" for k, x in enumerate(p["notes"], 1)] + [""]
    dest = Path(out_path) if out_path else src.with_name("大纲-审核报告.md")
    dest.write_text("\n".join(L), encoding="utf-8")
    print(f"已写入 {dest}：{len(pages)} 页")
    return 0

def cmd_deck_pdf(deck, out_path=None):
    """Gate 2：把 out/slides/*.png 拼成一份 PDF——像 PPT 一样翻着审。"""
    from PIL import Image
    out = out_dir(deck)
    ps = sorted((out / "slides").glob("*.png"))
    if not ps:
        sys.exit("还没有截图，先跑 review")
    imgs = [Image.open(p).convert("RGB") for p in ps]
    dest = Path(out_path) if out_path else Path(deck).resolve().parent / f"{Path(deck).stem}-预览.pdf"
    imgs[0].save(dest, save_all=True, append_images=imgs[1:], resolution=120)
    print(f"已写入 {dest}：{len(imgs)} 页（{dest.stat().st_size // 1024} KB）")


def cmd_review_doc(deck, out, v=None, out_path=None):
    """Gate 2：逐页解说词审定稿——画面要点 / 讲述要点（降序）/ 解说词 / 字数 vs 目标 / lint。
    lint 只卡下限，版本模式下上限就是时长控制本身，所以这里按 narrate 的同一容忍度补一条上限检查。"""
    tdir = v["dir"] if v else Path(out)
    load_pron(out, Path(out).parent)
    try:
        slides = load_slides(deck, out)
    except SystemExit:
        raise
    except Exception as e:                     # 坏 PPTX / 缺 slides.json 等：给人话，不要 traceback
        sys.exit(f"读不了这份幻灯片（{type(e).__name__}: {str(e)[:80]}）。\n"
                 f"  路线 B 请先跑 import；或确认文件没损坏、是幻灯片版式（不是 A4 报告）")
    cps = load_cps(out)
    secs = page_seconds(slides, v["minutes"] * 60, v["skip"]) if v else None
    nj = tdir / "narrations.json"
    if not nj.exists():
        sys.exit(f"还没有解说词：{nj}")
    narr = json.loads(nj.read_text(encoding="utf-8"))
    iters = sorted(secs, key=int) if secs else range(1, len(slides) + 1)
    rows = ["| 页 | 标题 | 时长 | 目标字数 | 实际 | lint |", "|---|---|---|---|---|---|"]
    _rvd = tdir / "review.md"
    try:                                       # 单版本下限：读 review.md 里生效的 --length
        _glo = int(str(parse_review(_rvd)[0].get("length") or DEFAULTS["length"]).split("-")[0]) \
            if _rvd.exists() else int(DEFAULTS["length"].split("-")[0])
    except Exception:
        _glo = int(DEFAULTS["length"].split("-")[0])
    blocks, tot, tot_t, nbad = [], 0, 0, 0
    for i in iters:
        i = int(i)
        s = slides[i - 1]
        lo = hi = None
        if secs:
            lo, hi = char_range(secs[i], cps, strict=True)
        else:
            _content = not slide_is_non_content(s, i, len(slides))
            lo = page_floor(s["sec"], _glo, cps, content=_content)
        txt = narr.get(str(i), "").strip()
        n = len(re.sub(r"\s", "", txt))
        grounded = grounding(out, tdir, s)
        iss = lint(txt, lo, grounded, disc=s.get("disc", False))
        if hi and n > hi * OVER_TOL:
            iss.append(f"字数超出上限（{n} > {hi}）")
        nbad += bool(iss)
        tot += n
        rng = f"{lo}–{hi}" if hi else f"≥{lo}"
        tot_t += ((lo + hi) // 2 if secs else lo)      # 目标取区间中值，上限只是容忍边界
        rows.append(f"| {i} | {s['title']} | {secs[i]:.0f}s | {rng} | {n} | "
                    f"{'✔' if not iss else '✘ ' + '；'.join(iss)} |" if secs else
                    f"| {i} | {s['title']} | — | {rng} | {n} | {'✔' if not iss else '✘ ' + '；'.join(iss)} |")
        tog = len(re.sub(r"\s", "", "\n".join(s["text"])))
        b = ["---", "", f"## 第 {i} 页 · {s['title']}",
             (f"`{secs[i]:.1f}s` · " if secs else "")
             + f"字数目标 **{rng}** · 实际 **{n}**"
             + (" · `data-disclaimer`" if s.get("disc") else "")
             + (" · `data-fixed`" if s.get("fixed") else ""), "",
             f"**画面（{tog} 字）**：{' / '.join(s['text'])[:300]}", ""]
        if s["notes"]:
            b += ["**讲述要点（降序）**", ""] + [f"{k}. {x}" for k, x in enumerate(s["notes"], 1)] + [""]
        b += ["**解说词**", "", "> " + txt.replace("\n", "\n> "), ""]
        if iss:
            b += [f"⚠️ lint：{'；'.join(iss)}", ""]
        blocks += b
    L = [f"# {Path(deck).stem} · 解说词审定稿（{v['name'] if v else '单版本'}）", "",
         (f"- 目标时长 **{v['minutes']} 分钟** · " if v else "")
         + f"{len(list(iters))} 页 · 假定语速 {cps} 字/秒",
         f"- 合计 **{tot} 字**" + (f" / 目标 {tot_t}" if v else "")
         + (f" · **lint 未过 {nbad} 页**" if nbad else " · lint 全绿"),
         "- **怎么改**：直接改本文件的「解说词」段落并告诉我，或改 narrations.json 后重跑本命令。", "",
         *rows, ""]
    dest = Path(out_path) if out_path else Path(deck).resolve().parent / f"解说词审定稿-{v['name'] if v else 'default'}.md"
    dest.write_text("\n".join(L + blocks), encoding="utf-8")
    print(f"已写入 {dest}：{tot} 字" + (f" / 目标 {tot_t}" if v else "")
          + (f"，lint 未过 {nbad} 页" if nbad else "，lint 全绿"))
    return 1 if nbad else 0


def cmd_qc(deck, out, v=None, out_path=None):
    """Gate 3：质检报告。不依赖 ffprobe——时长读 narration.wav，逐页时长读 list.ffconcat。"""
    tdir = v["dir"] if v else Path(out)
    ffc = tdir / "list.ffconcat"
    wav = tdir / "narration.wav"
    if not ffc.exists() or not wav.exists():
        sys.exit(f"先出片（缺 {ffc.name} / {wav.name}）")
    slides = load_slides(deck, out)
    secs = page_seconds(slides, v["minutes"] * 60, v["skip"]) if v else None
    with wave.open(str(wav)) as w:
        total = w.getnframes() / w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    peak, rms = float(np.abs(a).max()), float(np.sqrt((a ** 2).mean()))
    # 逐页时长：从 ffconcat 里取 duration（图片行与 duration 行成对）
    durs, cur = {}, None
    for line in ffc.read_text(encoding="utf-8").splitlines():
        m1 = re.search(r"file '.*?(\d{3})\.png'", line)
        m2 = re.search(r"^duration ([\d.]+)", line)
        if m1:
            cur = int(m1.group(1))
        elif m2 and cur:
            durs[cur] = durs.get(cur, 0) + float(m2.group(1))
            cur = None
    L = [f"# {Path(deck).stem} · 成片质检报告（{v['name'] if v else '单版本'}）", "",
         f"- 实际总时长 **{total / 60:.2f} 分钟**（{total:.1f} 秒）"
         + (f" · 目标 {v['minutes']} 分钟 · 偏差 **{total / (v['minutes'] * 60) - 1:+.0%}**" if v else ""),
         f"- 音量：峰值 **{peak:.3f}**（{20 * np.log10(max(peak, 1e-6)):.1f} dBFS）· RMS {rms:.4f}"
         + ("  ⚠ 偏低，检查配音" if peak < 0.05 else "  ✔ 正常"),
         f"- 页数：{len(durs)} 页有音轨", "",
         "| 页 | 标题 | 目标 | 实际 | 偏差 | 重录 |", "|---|---|---|---|---|---|"]
    bad = []
    for i in sorted(durs):
        s = slides[i - 1]
        tgt = secs.get(i) if secs else s.get("sec")
        d = durs[i]
        dev = f"{d / tgt - 1:+.0%}" if tgt else "—"
        if tgt and d / tgt < 0.8:
            bad.append(i)
        L.append(f"| {i} | {s['title']} | {f'{tgt:.0f}s' if tgt else '—'} | {d:.1f}s | {dev} | [ ] |")
    L += ["", f"**逐页时长明显偏短（< 80% 目标）**：{bad or '无'}", "",
          "## 字幕抽样", ""]
    srt = (tdir / "subs.srt").read_text(encoding="utf-8") if (tdir / "subs.srt").exists() else ""
    cues = re.findall(r"\d+\n([\d:,]+) --> ([\d:,]+)\n(.+?)\n", srt)
    L += [f"- 共 **{len(cues)}** 条字幕", ""]
    sample = cues if len(cues) <= 6 else cues[:3] + cues[-2:]   # 短字幕表别重复抽首尾
    for a_, b_, c_ in sample:
        L += [f"- `{a_}` → `{b_}`  {c_}", ""]
    L += ["## 重录", "",
          "在上面表格的「重录」列打 `[x]`，或直接告诉我「第 N 页重录，因为……」。"
          "只重合成被勾的页（句子级缓存会让其余页命中原音频）。", "",
          "## 怎么复核", "",
          "- 音色是否一致、有没有念错字：听片子；用 `redo` 表标记问题页。",
          "- 字幕与语音是否对齐：抽 3 处跳转核对（片头 / 中间 / 片尾）。",
          f"- 成片文件：`{tdir}/final-{v['name'] if v else 'video'}.mp4`"]
    dest = Path(out_path) if out_path else tdir / "质检报告.md"
    dest.write_text("\n".join(L), encoding="utf-8")
    print(f"已写入 {dest}：实际 {total / 60:.2f} 分钟"
          + (f" / 目标 {v['minutes']} 分钟" if v else "") + f"，偏短页 {bad or '无'}")


# ─── 审核台（review-ui）：本机起服务，只读展示 + 白名单写回 ──────────
# 由 skill 在到达审核点时按需拉起，前台阻塞到 owner 点「完成」为止。
# 它不做创作、不跑 pipeline：只把三个 gate 的产物渲染成可审界面，
# 并把 owner 的改动写回真源文件（outline.json / narrations.json / redo.json）。

def _ffconcat_durs(ffc):
    """从 list.ffconcat 里取每页时长（图片行 + duration 行成对）。不依赖 ffprobe。"""
    durs, cur = {}, None
    for line in Path(ffc).read_text(encoding="utf-8").splitlines():
        m1 = re.search(r"file '.*?(\d{3})\.(?:png|jpg|jpeg)'", line)
        m2 = re.match(r"duration ([\d.]+)", line)
        if m1:
            cur = int(m1.group(1))
        elif m2 and cur:
            durs[cur] = durs.get(cur, 0.0) + float(m2.group(1))
            cur = None
    return durs


def _wav_stats(wav):
    """音轨总时长 / 峰值 / RMS。用 wave 读，不需要 ffprobe。"""
    with wave.open(str(wav)) as w:
        total = w.getnframes() / w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    return total, float(np.abs(a).max()), float(np.sqrt((a ** 2).mean()))


def _outline_budget(o, cps=None):
    """outline.json → 每页分配秒数与字数区间（与出片时同一套 allocate/chars_for）。"""
    cps = cps or float(os.environ.get("CPS", "4.5"))
    fake = [{"title": p.get("title", ""), "sec": p.get("weight"), "fixed": p.get("fixed"),
             "disc": bool(p.get("disc")), "text": p.get("points", []), "notes": p.get("notes", [])}
            for p in o.get("pages", [])]
    secs = allocate(fake, float(o.get("minutes") or 14) * 60)
    out = []
    for i, p in enumerate(o.get("pages", []), 1):
        c = chars_for(secs[i], cps)
        _cr = char_range(secs[i], cps, strict=True)
        out.append({"n": i, "title": p.get("title", ""), "kind": p.get("kind", ""),
                    "weight": p.get("weight"), "fixed": p.get("fixed"), "disc": bool(p.get("disc")),
                    "sec": round(secs[i], 1), "lo": _cr[0], "hi": _cr[1]})
    return out, cps


# ─── 大纲.md（Gate 1 的真源：导演剧本，模型据此做 slides）─────────────
# md 是人写的、也是模型读的；工具只从里面抠它需要的几个数（页号/页长），
# 其余内容原样保留。审核台把它按页拆开显示，保存时把那一段原样写回。

OUTLINE_MD = "大纲.md"

# 大纲格式以 owner 的规范为准（business-deck-spec.md §一 第 17–30 行的示例）：
#   ## P5 <结论式标题 ≤30 字>
#   - 版式：KPI 条(3) + 双栏要点(3+3) + 结论框
#   - 信息点（10）：
#     1. …
#   - 时长：45 秒
#   - 讲述要点（3–5 条，每条注明出处）：
#     1. …（§3.1）
# 本文件只解析这几个标记；其余内容原样保留、不解释。
RE_PAGE_MD = re.compile(r"^##\s*P\s*(\d+)\s*[.、:：]?\s*(.+?)\s*$")
RE_PAGE_COMPAT = re.compile(r"^###\s*第\s*(\d+)\s*页\s*[·:：]\s*(.+?)\s*$")   # 旧格式，兼容读取
RE_SEC = re.compile(r"\*{0,2}时长\*{0,2}\s*[：:]\s*(\d+(?:\.\d+)?)\s*秒")
RE_INFO_HDR = re.compile(r"\*{0,2}信息点\*{0,2}\s*[（(]\s*(\d+)\s*[)）]\s*[：:]?")
RE_ITEM_NUM = re.compile(r"^\s*(?:\d+[.、)）]|[-*])\s+(.+)$")
# 出处标注可以有多种写法：规范的例子是（§3.1），实际源文档常用章节号，
# 所以（源文档 二、（二））、（第 3 页）、（表 11）都算注明了出处。
RE_CITE = re.compile(r"[（(]\s*(?:§|第|源文档|表|图|附录|原文)\s*[^）)]{0,40}[)）]")
NON_CONTENT = ("封面", "目录", "章节", "结尾", "封底", "致谢")


def _md_field(chunk, *names):
    """取 `- 名称：值` 或 `- **名称**：值` 的值。"""
    for nm in names:
        m = re.search(rf"^-\s*\*{{0,2}}{nm}\*{{0,2}}\s*[：:]\s*(.*)$", chunk, re.M)
        if m:
            return m.group(1).strip()
    return ""


def _md_list(chunk, *names):
    """取某个字段下面的编号/项目列表。"""
    for nm in names:
        m = re.search(rf"^-\s*\*{{0,2}}{nm}\*{{0,2}}[^\n]*[：:]\s*$", chunk, re.M)
        if not m:
            continue
        rest = chunk[m.end():]
        out = []
        for ln in rest.split("\n"):
            if not ln.strip():
                if out:
                    break
                continue
            if ln.lstrip().startswith("-"):        # 遇到下一个字段（`- xxx：`）就停
                break
            im = RE_ITEM_NUM.match(ln)
            if im:
                out.append(im.group(1).strip())
            elif out and ln[:1] in (" ", "\t"):
                out[-1] += " " + ln.strip()
            else:
                break
        return out
    return []


def parse_outline_md(text):
    """按规范格式拆页（并兼容旧的 `### 第 NN 页 ·` 写法）。

    每页保留原始 md 片段（chunk），审核台编辑后原样写回。
    """
    lines = text.split("\n")
    n, pages, pre, i = len(lines), [], [], 0
    while i < n:
        m = RE_PAGE_MD.match(lines[i]) or RE_PAGE_COMPAT.match(lines[i])
        if m:
            j = i + 1
            while j < n and not RE_PAGE_MD.match(lines[j]) and not RE_PAGE_COMPAT.match(lines[j]):
                j += 1
            chunk = "\n".join(lines[i:j]).rstrip()
            no, title = int(m.group(1)), m.group(2).strip()
            sec = RE_SEC.search(chunk)
            decl = RE_INFO_HDR.search(chunk)
            pages.append({
                "n": no, "title": title, "chunk": chunk, "part": "",
                "layout": _md_field(chunk, "版式", "骨架"),
                "sec": float(sec.group(1)) if sec else None,
                "info": _md_list(chunk, r"信息点"),
                "info_declared": int(decl.group(1)) if decl else None,
                "notes": _md_list(chunk, r"讲述要点", r"演讲备注"),
                "dropped": _md_field(chunk, "舍弃"),
                "weight": None, "fixed": None, "disc": False,
            })
            i = j
            continue
        if not pages:
            pre.append(lines[i])
        i += 1
    return {"preamble": "\n".join(pre).rstrip(), "parts": [], "pages": pages}


def renumber_outline_md(text):
    """按文档顺序把页号重编成 P1..Pn。"""
    out, k = [], 0
    for ln in text.split("\n"):
        m = RE_PAGE_MD.match(ln) or RE_PAGE_COMPAT.match(ln)
        if m:
            k += 1
            out.append(f"## P{k} {m.group(2)}")
        else:
            out.append(ln)
    return "\n".join(out)


def _page_span(lines, n):
    """第 n 页在行数组里的 [起, 止) —— 到下一页为止。"""
    for i, l in enumerate(lines):
        m = RE_PAGE_MD.match(l) or RE_PAGE_COMPAT.match(l)
        if m and int(m.group(1)) == n:
            j = i + 1
            while j < len(lines) and not RE_PAGE_MD.match(lines[j]) and not RE_PAGE_COMPAT.match(lines[j]):
                j += 1
            return i, j
    return None


def replace_page_md(text, n, chunk):
    """把第 n 页那一段替换掉，其他字节不动。n=0 表示文件头部。"""
    lines = text.split("\n")
    if n == 0:
        for i, ln in enumerate(lines):
            if RE_PAGE_MD.match(ln) or RE_PAGE_COMPAT.match(ln):
                return "\n".join(chunk.split("\n") + lines[i:])
        return chunk
    sp = _page_span(lines, n)
    if sp is None:
        return None
    s, e = sp
    return "\n".join(lines[:s] + chunk.split("\n") + lines[e:])


def outline_md_op(text, op, n=0):
    """大纲结构操作：add / del / up / down。返回新文本；不可行返回 None。"""
    p = parse_outline_md(text)
    nums = [x["n"] for x in p["pages"]]
    lines = text.split("\n")
    if op == "del":
        sp = _page_span(lines, n)
        return renumber_outline_md("\n".join(lines[:sp[0]] + lines[sp[1]:])) if sp else None
    if op in ("up", "down"):
        if n not in nums:
            return None
        i = nums.index(n)
        j = i - 1 if op == "up" else i + 1
        if j < 0 or j >= len(nums):
            return None
        a, b = _page_span(lines, nums[i]), _page_span(lines, nums[j])
        (s1, e1), (s2, e2) = (a, b) if a[0] < b[0] else (b, a)
        return renumber_outline_md("\n".join(lines[:s1] + lines[s2:e2] + lines[s1:e1] + lines[e2:]))
    if op == "add":
        stub = ("## P00 新页（标题写成结论，≤30 字）\n"
                "- 版式：\n"
                "- 信息点（0）：\n  1. \n"
                "- 时长：45 秒\n"
                "- 讲述要点（3–5 条，每条注明出处）：\n  1. （§）\n")
        if n in nums:
            sp = _page_span(lines, n)
            return renumber_outline_md("\n".join(lines[:sp[1]] + stub.split("\n") + lines[sp[1]:]))
        return renumber_outline_md(text.rstrip() + "\n\n" + stub)
    return None


def outline_md_budget(parsed, cps=None):
    """每页 → 目标字数区间。

    字数公式照规范 §八 第 104 行：`data-sec × CPS`（CPS 默认 4.5）；
    区间照规范 §八 第 106 行：0.8–1.3×。
    """
    cps = cps or float(os.environ.get("CPS", "4.5"))
    m = re.search(r"目标时长\s*[：:]\s*([\d.]+)\s*分钟", parsed["preamble"])
    minutes = float(m.group(1)) if m else None
    out = []
    for p in parsed["pages"]:
        sec = p["sec"] or 45
        out.append({"n": p["n"], "title": p["title"], "kind": "", "weight": None,
                    "fixed": None, "disc": False, "sec": sec,
                    "lo": char_range(sec, cps)[0], "hi": char_range(sec, cps)[1]})
    return out, cps, minutes


# 页型判断启发式（**这是我加的，规范没定义页型字段**）：
# 先看「版式」里的模板名（（模板 cover.html）这类），再看标题/版式里的词，
# 最后按规范结构把首页当封面、末页当结尾。
NON_CONTENT_TPL = {"cover", "toc", "divider", "section-divider", "section",
                   "end", "ending", "thanks", "closing", "cta", "back"}


def is_non_content(p, idx=None, total=None):
    blob = f"{p['title']} {p['layout']}"
    m = re.search(r"模板\s*([A-Za-z0-9._-]+)", blob)
    tpl = (m.group(1) if m else "").lower()
    tpl = re.sub(r"\.html?$", "", tpl)
    if tpl in NON_CONTENT_TPL or any(k in blob for k in NON_CONTENT):
        return True
    if idx is not None and total and (idx == 0 or idx == total - 1):
        return True
    return False



# ─── 画布容量估算：设计阶段（Gate ①）就能算出「这页装不装得下」─────────────
# 常量来源：dailei-business-deck 的 CSS 几何 + 规范 §三 的字号，
# 并用 2026-10-07 的 30 页真实素材逐页实测标定（表格高度误差 ≤2px）。
# 目的：**不要等渲染完才发现内容高于画布** —— 大纲阶段就算出来，改大纲而不是改 HTML。
GEOM = {
    "body": 922,          # 正文区（1080 − band 84 − foot 74）
    "pad_top": 40,        # .bd-body 上内边距
    "pad_bottom": 52,     # 字幕安全区留白（底部 120px − footer 74px + 6）
    "title": 141,         # kicker 33 + h1 74 + rule 3 + 间距 31
    "comp_margin": 24,    # 组件与标题块之间的间距（多数组件 20–28）
    "line": {"table": 46.4, "th": 47.0, "body": 52.0, "caption": 38.0, "li": 46.0},
    "box": {"th": 76.0, "td": 71.0, "cap": 12.0},   # 一行时的基准高（含 padding）
    "font": {"table": 32, "body": 40, "label": 28, "caption": 28, "kpi": 88},
}


def text_width(t):
    """估算字符串占宽（px）：CJK 按字号 1.0，ASCII 按 0.55。"""
    w = 0.0
    for ch in str(t):
        w += 1.0 if ord(ch) > 0x2E7F else 0.55
    return w


def est_lines(text, width_px, font_px, limit=None):
    """按可用宽度估算行数（至少 1）。"""
    if not str(text).strip():
        return 1
    n = max(1, int(text_width(text) * font_px / max(width_px, 1)))
    return min(n, limit) if limit else n


def table_height(rows_items, ncol, avail_w=1728):
    """表格自然高度（实测标定：一行 71px，每多一行 +46.4px；表头 76/123px）。

    列宽是浏览器**按内容自动分配**的：先算每列理想宽，总和超画布就按比例压缩，
    被压到放不下的格才会换行 —— 这是实测（2026-10-07，30 页真实素材）得到的规律，
    等分列宽的估法会严重高估行数（平均误差 84px → 该模型 ≤ …）。"""
    if not rows_items:
        return 0.0
    f = GEOM["font"]["table"]
    colw = []
    for j in range(ncol):
        ideal = max([text_width(r[j]) * f for r in rows_items if j < len(r)] + [0.0]) + 44
        colw.append(max(ideal, 80.0))
    total = sum(colw)
    scale = min(1.0, avail_w / total) if total else 1.0
    colw = [w * scale for w in colw]

    def lines_of(cells):
        n = 1
        for j, c in enumerate(cells):
            room = max(colw[j] - 44, 20.0)
            w = text_width(c) * f
            n = max(n, 1 if w <= room else int(-(-w // room)))
        return n

    head, *body = rows_items
    nl = [lines_of(r) for r in body]
    h = GEOM["box"]["th"] + GEOM["line"]["th"] * (lines_of(head) - 1)
    for n_ in nl:
        h += GEOM["box"]["td"] + GEOM["line"]["table"] * (n_ - 1)
    return h


def estimate_page(page):
    """估算一页的占用高度与余量（正数=还空着，负数=内容高于画布）。

    返回 {"used":…, "slack":…, "note":…}；只做算术，不渲染。"""
    layout = page.get("layout") or ""
    info = [re.sub(r"（占 \d+ 行）\s*$", "", str(x)).strip() for x in page.get("info", [])]
    used = GEOM["pad_top"] + GEOM["title"] + GEOM["comp_margin"] + GEOM["pad_bottom"]
    note = ""

    tpl = re.search(r"模板\s*([a-z-]+)\.html", layout)
    tpl = tpl.group(1) if tpl else "table"
    if tpl == "table":
        head, rows, caps = [], [], []
        for it in info:
            m = re.match(r"^表头[：:]\s*(.+)$", it)
            if m:
                head = [x.strip() for x in m.group(1).split("·")]
                continue
            if re.match(r"^(图注|说明)[：:]", it):
                caps.append(re.sub(r"^(图注|说明)[：:]\s*", "", it))
                continue
            if re.match(r"^(结论框|结论)[：:]", it):
                continue
            rows.append([x.strip() for x in it.split("·")])
        ncol = max(len(head), max((len(r) for r in rows), default=1))
        h = table_height(([head] if head else [[""] * ncol]) + rows, ncol)
        if caps:
            h += GEOM["box"]["cap"] + GEOM["line"]["caption"] * est_lines(
                "；".join(caps), 1728, GEOM["font"]["caption"])
        used += h
        note = f"表 {ncol} 列 × {len(rows)} 行"
    elif tpl == "chart":
        used += 560 + 28 + GEOM["line"]["caption"] * 2
        note = "图表固定高"
    elif tpl in ("bullets",):
        used += sum(GEOM["line"]["body"] * est_lines(x, 1600, GEOM["font"]["body"]) + 13 for x in info)
        note = f"{len(info)} 条要点"
    elif tpl in ("two-column", "comparison", "three-column"):
        per = 1 if tpl == "two-column" else (2 if tpl == "comparison" else 3)
        col_w = 1728 / per - 88
        lines = sum(est_lines(x, col_w, GEOM["font"]["body"]) for x in info)
        used += max(lines, 1) * GEOM["line"]["body"] / max(per, 1) + 120
        note = f"{len(info)} 条 / {per} 栏"
    elif tpl == "kpi-grid":
        used += 300 + (GEOM["line"]["caption"] * 2 if len(info) > 4 else 0)
        note = "KPI 条固定高"
    elif tpl in ("end",):
        used += GEOM["line"]["body"] * len(info) + 120
    else:                                    # cover / toc / divider：满版，不受限
        return {"used": 0, "slack": 999, "note": "满版页（不受正文高度限制）"}
    return {"used": int(used), "slack": int(GEOM["body"] - used), "note": note}




def split_cells(line):
    """按「 · 」切表格列，**忽略括号内的分隔符**。
    实测踩坑：`发光层整体方案（RD／GD／BD · RH／GH／BH · R'G'B'）` 曾被切坏成 3 列。"""
    parts, depth, cur, i = [], 0, "", 0
    while i < len(line):
        ch = line[i]
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if depth == 0 and line[i:i + 3] == " · ":
            parts.append(cur.strip()); cur = ""; i += 3; continue
        cur += ch; i += 1
    parts.append(cur.strip())
    return [x for x in parts if x != ""]


def capacity_issues(page):
    """按规范 §三 第 61 行的容量表，在设计阶段就判定「这一页装不下」。

    规范给的容量参考（这是 owner 定的设计预算，不是猜的）：
      单栏正文每行约 40 字、约 12 行；双栏每栏每行约 18 字、约 10 行；
      表格 ≤4 列、每格约 12 字、表头 + 8 行；KPI 条 3–4 个。
    超了就按规范 §三 第 62 行的顺序处理：精简措辞 → 换版式 → 拆页。
    返回 [(级别, 说明), …]。"""
    out = []
    layout = page.get("layout") or ""
    info = [re.sub(r"（占 \d+ 行）\s*$", "", str(x)).strip() for x in page.get("info", [])]
    tpl = re.search(r"模板\s*([a-z-]+)\.html", layout)
    tpl = tpl.group(1) if tpl else ""
    if tpl == "table":
        head, rows, caps = [], [], []
        for it in info:
            m = re.match(r"^表头[：:]\s*(.+)$", it)
            if m:
                head = split_cells(m.group(1))
                continue
            if re.match(r"^(图注|说明|结论框|结论)[：:]", it):
                if re.match(r"^(图注|说明)[：:]", it):
                    caps.append(re.sub(r"^(图注|说明)[：:]\s*", "", it))
                continue
            cells = split_cells(it)
            if len(cells) == 1 and rows and len(rows[-1]) < len(head or rows[-1]) + 1:
                rows[-1].append(cells[0])          # 续行：并进上一行（P18 的写法）
            else:
                rows.append(cells)
        if head and rows:
            bad = [(k + 1, len(r)) for k, r in enumerate(rows) if len(r) != len(head)]
            if bad:
                out.append(("ERROR", f"表头 {len(head)} 列，但第 {[b[0] for b in bad]} 行是 "
                                     f"{[b[1] for b in bad]} 列 —— 画面会错位；"
                                     "一行的各列用「 · 」分隔，格内列举用「／」或「、」"))
        ncol = max(len(head), max((len(r) for r in rows), default=1))
        if ncol > 4:
            out.append(("ERROR", f"表格 {ncol} 列 > 规范上限 4 列"))
        # 行数上限按几何算，而不是照抄规范里的"8 行"——那个数**没算图注**。
        # 实测（2026-10-07 真实素材）：正文可用 922 − 上下留白 92 − 标题块 141 − 组件间距 24 = 665px；
        # 表头 76px、每行 71px、图注一行 50px（含间距）→ 有图注最多 7 行，无图注 8 行。
        avail = GEOM["body"] - GEOM["pad_top"] - GEOM["pad_bottom"] - GEOM["title"] - GEOM["comp_margin"]
        room = avail - GEOM["box"]["th"] - (50 if caps else 0)
        max_rows = int(room // GEOM["box"]["td"])
        if len(rows) > max_rows:
            why = "（含图注）" if caps else ""
            out.append(("ERROR",     # 超出几何上限 = 一定溢出，不是"可能"
                        f"表格 {len(rows)} 行超出画布{why}：几何上限 {max_rows} 行"
                        f"（可用 {avail:.0f}px − 表头 76 − 图注 {50 if caps else 0}）→ 砍 "
                        f"{len(rows) - max_rows} 行，或去掉图注，或按规范 §三 第 62 行换版式/拆页"))
        over = [(len(c), c) for r in rows for c in r if len(c) > 12]
        if over:
            n, longest = max(over)
            cpl = max(7, int((1728 / max(ncol, 1) - 44) / 32))     # 实测标定：一行能放多少字
            lines_ok = 3 if len(rows) <= 3 else (2 if len(rows) == 4 else 1)
            out.append(("WARN", f"{len(over)} 个格超过容量（最长 {n} 字：{longest[:16]}…）："
                                f"{ncol} 列 × {len(rows)} 行时每格约 {cpl * lines_ok} 字"
                                f"（一行 {cpl} 字 × {lines_ok} 行）—— 做 slides 时会被精简，"
                                "建议直接在大纲里改短，或按规范 §三 第 62 行换版式/拆页"))
        if caps and sum(len(c) for c in caps) > 60:
            out.append(("WARN", f"图注合计 {sum(len(c) for c in caps)} 字 > 一行约 60 字"
                                "—— 画面上只放一行，建议精简或合并"))
    elif tpl in ("bullets", "two-column"):
        per = 1 if tpl == "bullets" else 2
        cpl = 40 if per == 1 else 18
        lines = sum(max(1, int(-(-len(x) // cpl))) for x in info)
        cap_lines = (12 if per == 1 else 10) * per
        if lines > cap_lines:
            out.append(("WARN", f"正文约 {lines} 行 > 容量 {cap_lines} 行（每行约 {cpl} 字）"))
    elif tpl == "kpi-grid":
        n = len([x for x in info if re.search(r"\d", x)])
        if n > 4:
            out.append(("WARN", f"KPI 条 {n} 个 > 规范 3–4 个"))
    return out


def outline_preflight(parsed, src_text=None):
    """Gate 1 预检。**注意：规范没有要求这套自动检查，这是我加的便利校验**
    （规范只定义了格式与密度规则）。级别：ERROR 必须改，WARN 请人确认。"""
    issues = []
    add = lambda lv, msg, n=None: issues.append({"level": lv, "page": n, "msg": msg})
    pages = parsed["pages"]
    if not pages:
        return [{"level": "ERROR", "page": None, "msg": "没解析到任何页：页标题要写成『## P5 结论式标题』"}]
    nos = [p["n"] for p in pages]
    if nos != list(range(1, len(pages) + 1)):
        add("WARN", f"页号不连续：{nos}")
    for k in ("受众", "目标时长", "主线", "素材"):
        if not re.search(rf"^-\s*{k}\s*[：:]", parsed["preamble"], re.M):
            add("WARN", f"文件开头缺『- {k}：』")
    secs = [p["sec"] for p in pages]
    if any(s is None for s in secs):
        add("ERROR", f"有页缺『- 时长：N 秒』：{[p['n'] for p in pages if p['sec'] is None]}")
    m = re.search(r"目标时长\s*[：:]\s*([\d.]+)\s*分钟", parsed["preamble"])
    if m and all(secs):
        total, want = sum(secs), float(m.group(1)) * 60   # 仅作规划参考，不判定
    src_nums = set(re.findall(r"\d[\d,]*(?:\.\d+)?", src_text)) if src_text else None
    for i, p in enumerate(pages):
        n, t = p["n"], p["title"]
        non_content = is_non_content(p, i, len(pages))
        if len(t) > 30:
            add("WARN", f"标题 {len(t)} 字 > 30（规范 §一 第 14 行：≤30 字一行）", n)
        if not p["layout"]:
            add("ERROR", "缺『- 版式：』", n)
        if not non_content:
            cnt = p["info_declared"] if p["info_declared"] is not None else len(p["info"])
            if not 6 <= cnt <= 10:
                add("ERROR" if cnt > 10 else "WARN",
                    f"信息点 {cnt} 个（规范 §二 第 41 行：内容页 6–10，>10 必须拆页）", n)
            if p["info_declared"] is not None and p["info"] and p["info_declared"] != len(p["info"]):
                add("ERROR", f"『信息点（{p['info_declared']}）』与实际 {len(p['info'])} 条不符", n)
        k = len(p["notes"])
        if not 3 <= k <= 5:
            add("WARN", f"讲述要点 {k} 条（规范 §一 第 26 行：3–5 条）", n)
        # 只卡「含数字/事实但没出处」的要点——出处的用途是给解说词溯源（规范 §八 第 102 行），
        # 不是给每句话挂来源；出处写在幕后要点里，不进画面（规范 §一 第 26 行「画面上不写」）。
        no_cite = [x for x in p["notes"] if re.search(r"\d", x) and not RE_CITE.search(x)]
        if no_cite:
            add("WARN", f"{len(no_cite)} 条含数字的讲述要点没注明出处（§一 第 26 行；出处只写在幕后要点，不进画面）", n)
        # 画布容量（规范 §三 第 61 行的容量表）：设计阶段就判定这页装不装得下
        for _lv, _msg in capacity_issues(p):
            add(_lv, _msg, n)
        blob = "\n".join([t, p["layout"], p["chunk"]])
        # 剥掉出处标注：`（源文档 免责声明 1）` 是在引用源文档的小节名，不是本页写了免责
        blob = re.sub(r"[（(][^）)]{0,40}(?:源文档|§|第\s*\d)[^）)]{0,40}[）)]", " ", blob)
        if re.search(r"免责|不构成.{0,8}(承诺|建议)|风险提示|声明.{0,4}性质", blob):
            add("ERROR", "出现免责/声明类内容（owner 要求默认不加）", n)
        if src_nums is not None:
            blob = "\n".join([t] + p["info"])
            blob = re.sub(r"[（(]\s*占\s*\d+\s*行\s*[)）]", "", blob)     # 计数标注不算数字
            blob = re.sub(r"序号\s*\d+", " ", blob)                        # 章节序号不是数据
            blob = RE_CITE.sub("", blob)
            miss = [x for x in set(re.findall(r"\d[\d,]*(?:\.\d+)?", blob))
                    if x not in src_nums and x.replace(",", "") not in src_nums]
            if miss:
                add("WARN", f"这些数字没在源文档里逐字查到：{', '.join(miss[:6])}", n)
    return issues


def cmd_review_ui(target, version=None, host="127.0.0.1", port=8099, gate=None, open_browser=True):
    import http.server, mimetypes, socket, threading, urllib.parse, webbrowser
    try:
        sys.stdout.reconfigure(line_buffering=True)   # 立刻看到 URL，否则重定向到文件时会被缓冲
    except Exception:
        pass

    tgt = Path(target).resolve()
    if not tgt.exists():
        sys.exit(f"没有 {tgt}")
    is_deck = tgt.suffix.lower() in (".html", ".htm", ".pptx", ".pdf")
    deck = str(tgt) if is_deck else (tgt.parent / "index.html" if (tgt.parent / "index.html").exists() else None)
    work = tgt.parent
    oj = (work / "outline.json") if not is_deck else ((work.parent / "outline.json") if (work.parent / "outline.json").exists() else (work / "outline.json"))
    out = out_dir(deck) if deck else work / "video-output"
    v = load_version(deck, out, version) if (version and deck) else None
    tdir = v["dir"] if v else out
    roots = [work, work.parent, out, tdir]
    tpl = Path(__file__).resolve().parent / "review-ui.html"
    if not tpl.exists():
        sys.exit(f"缺界面文件 {tpl}")
    import secrets
    # 绑非本机（tailnet/局域网）时必须有 token，否则同网段任何人都能 /save 写文件
    TOKEN = secrets.token_urlsafe(16) if host not in ("127.0.0.1", "localhost", "::1") else ""
    if TOKEN:
        print(f"⚠ 绑定在 {host}：已启用访问 token（不带 token 的请求一律 403）")

    def slides_of():
        if not deck:
            return []
        try:
            sl = load_slides(deck, out)
        except SystemExit:
            return []
        except Exception as e:                    # 坏 PPTX / 缺 slides.json：别让整个响应崩掉
            print(f"⚠ 读幻灯片失败（{type(e).__name__}: {str(e)[:60]}）："
                  "路线 B 请先跑 import；这里只显示大纲与解说词")
            return []
        ps = sorted((out / "slides").glob("*.png"))
        res = []
        for i, p in enumerate(ps, 1):
            res.append({"n": i, "url": "/file?p=" + urllib.parse.quote(str(p)),
                        "title": sl[i - 1]["title"] if i - 1 < len(sl) else ""})
        return res

    def narration_of(budget):
        nj = tdir / "narrations.json"
        if not nj.exists():
            return None
        narr = json.loads(nj.read_text(encoding="utf-8"))
        sl = load_slides(deck, out) if deck else []
        cps = float(os.environ.get("CPS", "4.5"))
        res = {}
        for b in budget:
            t = (narr.get(str(b["n"])) or "").strip()
            n = len(re.sub(r"\s", "", t))
            iss = []
            if deck and b["n"] - 1 < len(sl):
                s = sl[b["n"] - 1]
                grounded = grounding(out, tdir, s)
                iss = lint(t, b["lo"], grounded, disc=s.get("disc", False))
                if n > b["hi"] * OVER_TOL:
                    iss.append(f"字数超出上限（{n} > {b['hi']}）")
            res[str(b["n"])] = {"text": t, "n": n, "issues": iss}
        return res

    def video_of(budget):
        if not deck:
            return None
        cand = sorted(tdir.glob("final-*.mp4")) or sorted(tdir.glob("final-video.mp4"))
        if not cand or not (tdir / "narration.wav").exists():
            return None
        total, peak, rms = _wav_stats(tdir / "narration.wav")
        durs = _ffconcat_durs(tdir / "list.ffconcat") if (tdir / "list.ffconcat").exists() else {}
        sl = load_slides(deck, out)
        pages = []
        for b in budget:
            tgt_s = b["sec"]
            act = durs.get(b["n"])
            dev = f"{act / tgt_s - 1:+.0%}" if (act and tgt_s) else "—"
            pages.append({"n": b["n"], "title": b["title"], "target": f"{tgt_s:.0f}s",
                          "actual": f"{act:.1f}s" if act else "—", "dev": dev,
                          "short": bool(act and tgt_s and act / tgt_s < 0.8)})
        return {"url": "/file?p=" + urllib.parse.quote(str(cand[0])), "minutes": f"{total / 60:.2f} 分钟",
                "target": f"{v['minutes']} 分钟" if v else None,
                "dev": f"{total / (v['minutes'] * 60) - 1:+.0%}" if v else None,
                "peak": f"{20 * np.log10(max(peak, 1e-6)):.1f} dBFS", "pages": pages}

    state = {}
    # 目标本身是 .md 就直接当大纲；否则按约定名在附近找
    omd = tgt if tgt.suffix.lower() in (".md", ".markdown") else \
        next((c for c in (work / OUTLINE_MD, work.parent / OUTLINE_MD) if c.exists()), None)

    def build_state():
        o, parsed = None, None
        if omd is not None:
            parsed = parse_outline_md(omd.read_text(encoding="utf-8"))
        if oj.exists():
            try:
                o = json.loads(oj.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"⚠ outline.json 解析失败：{e}")
        if parsed and parsed["pages"]:
            budget, cps, mins = outline_md_budget(parsed)
        else:
            parsed = None
            budget, cps = _outline_budget(o) if o else ([], 4.5)
            mins = (o or {}).get("minutes")
        if not budget and deck:
            sl = load_slides(deck, out)
            secs = page_seconds(sl, v["minutes"] * 60, v["skip"]) if v else {}
            budget = [{"n": i + 1, "title": s["title"], "kind": "", "weight": s["sec"],
                       "fixed": s["fixed"], "disc": s["disc"], "sec": round(secs.get(i + 1, 0), 1),
                       "lo": char_range(secs.get(i + 1, 0), cps)[0],
                       "hi": char_range(secs.get(i + 1, 0), cps)[1]} for i, s in enumerate(sl)]
        _glo_ui = int(DEFAULTS["length"].split("-")[0])
        try:
            _rv_ui = tdir / "review.md"
            if _rv_ui.exists():
                _glo_ui = int(str(parse_review(_rv_ui)[0].get("length") or DEFAULTS["length"]).split("-")[0])
        except Exception:
            pass
        if deck:                                  # 解说词按 deck 的页出（拆页后大纲页数会少）
            try:
                _sl = load_slides(deck, out)
                _secs = page_seconds(_sl, (v["minutes"] * 60) if v else sum(
                    (s.get("sec") or 45) for s in _sl), v["skip"] if v else ())
                budget = []
                for _i, _s in enumerate(_sl, 1):
                    if v and _i in v["skip"]:
                        continue
                    _sec = _secs.get(_i, _s.get("sec") or 45)
                    _content = not slide_is_non_content(_s, _i, len(_sl))
                    _lo = page_floor(_sec, _glo_ui, cps, content=_content)
                    _hi = char_range(_sec, cps, strict=True)[1] if v else None
                    budget.append({"n": _i, "title": _s["title"], "kind": "", "weight": None,
                                   "fixed": None, "disc": bool(_s.get("disc")),
                                   "sec": round(_sec, 1), "lo": _lo, "hi": _hi})
            except Exception as e:
                print(f"⚠ 用 deck 生成解说词预算失败（{type(e).__name__}），退回大纲预算")

        # 预检问题按页带上：owner 在审核台里就能看到「这页装不下 / 行列不齐」
        outline_issues = {}
        try:
            for _is in outline_preflight(parsed):
                outline_issues.setdefault(_is.get('page'), []).append(
                    {'level': _is['level'], 'msg': _is['msg']})
        except Exception as e:
            print(f'⚠ 预检失败（{type(e).__name__}: {str(e)[:60]}）')

        state.clear()
        state.update({
            "name": Path(deck).stem if deck else tgt.stem, "target": str(tgt), "version": version,
            "bind": f"{host}:{port}", "gate": gate, "outline": o, "outline_md": parsed,
            "outline_md_text": omd.read_text(encoding="utf-8") if omd else None,
            "outline_md_path": str(omd) if omd else None, "minutes": mins, "budget": budget,
            "slides": slides_of(), "narration": narration_of(budget),
            "outline_issues": outline_issues,
            "video": video_of(budget), "ground": _ground_of(deck, out),
            "redo": json.loads((tdir / "redo.json").read_text(encoding="utf-8")) if (tdir / "redo.json").exists() else {},
            "cps": cps,
        })
        return state

    def _ground_of(deck, out):
        if not deck:
            return {}
        sl = load_slides(deck, out)
        return {str(i + 1): " / ".join(s["text"])[:400] for i, s in enumerate(sl)}

    changes = []
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a):
            pass
        def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def _authed(self, u):
            if not TOKEN:
                return True
            return urllib.parse.parse_qs(u.query).get("t", [""])[0] == TOKEN or \
                   self.headers.get("X-Review-Token") == TOKEN

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if not self._authed(u):
                return self._send(403, b'{"error":"need token"}')
            if u.path == "/":
                return self._send(200, tpl.read_bytes(), "text/html; charset=utf-8")   # 每次读，改界面不用重启
            if u.path == "/state":
                return self._send(200, json.dumps(build_state(), ensure_ascii=False).encode())
            if u.path == "/file":
                q = urllib.parse.parse_qs(u.query).get("p", [""])[0]
                p = Path(q).resolve()
                if not any(p.is_relative_to(Path(r).resolve()) for r in roots) or not p.is_file():
                    return self._send(403, b'{"error":"forbidden"}')
                ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                size = p.stat().st_size
                rng = self.headers.get("Range") or ""
                start, end, code = 0, size - 1, 200
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with open(p, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
                return
            self._send(404, b'{"error":"not found"}')
        def do_POST(self):
            _u = urllib.parse.urlparse(self.path)
            if not self._authed(_u):
                return self._send(403, b'{"error":"need token"}')
            path = _u.path
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, b'{"error":"bad json"}')
            g = "done" if path == "/done" else req.get("gate")     # /done 按路径认，不靠 body
            if g == "outline-md":                  # 大纲.md 写回（md 是真源）
                if omd is None:
                    return self._send(400, b'{"error":"no outline.md"}')
                txt = omd.read_text(encoding="utf-8")
                if req.get("full") is not None:    # 整篇替换（自由改页数/标题/顺序）
                    new = renumber_outline_md(req["full"].rstrip() + "\n")
                    if not parse_outline_md(new)["pages"]:
                        return self._send(400, json.dumps({"error": "改完解析不出任何页，检查 '### 第 NN 页 · 标题' 格式"}).encode())
                    txt = new
                    changes.append("大纲.md：整篇改写")
                elif req.get("op"):                # 结构操作：add / del / up / down
                    new = outline_md_op(txt, req["op"], int(req.get("n") or 0))
                    if new is None:
                        return self._send(400, json.dumps({"error": f"操作 {req['op']} 于第 {req.get('n')} 页不可行"}).encode())
                    txt = new
                    changes.append(f"大纲.md：{req['op']} 第 {req.get('n')} 页")
                else:                              # 逐页写回
                    pages = req.get("pages") or ({str(req["n"]): req.get("chunk", "")} if "n" in req else None)
                    if not pages:
                        return self._send(400, b'{"error":"nothing to save"}')
                    for k, chunk in pages.items():
                        head1 = (chunk or "").strip().split("\n")[0] if (chunk or "").strip() else ""
                        if head1 and not (RE_PAGE_MD.match(head1) or RE_PAGE_COMPAT.match(head1)):
                            return self._send(400, json.dumps(
                                {"error": f"第 {k} 页的片段第一行必须是页头（规范格式：'## P{k} 结论式标题'），"
                                          f"当前是：{head1[:40]}"}, ensure_ascii=False).encode())
                        new = replace_page_md(txt, int(k), (chunk or "").rstrip())
                        if new is None:
                            return self._send(400, json.dumps({"error": f"page {k} not found"}).encode())
                        txt = new
                    changes.append(f"大纲.md：改了 {len(pages)} 处")
                shutil.copy2(omd, omd.with_name(omd.name + ".bak"))
                omd.write_text(txt, encoding="utf-8")
                st = build_state()
                return self._send(200, json.dumps(st, ensure_ascii=False).encode())
            if g == "outline":
                o = req.get("outline") or {}
                if not o.get("pages"):
                    return self._send(400, b'{"error":"pages \xe4\xb8\x8d\xe8\x83\xbd\xe4\xb8\xba\xe7\xa9\xba"}')
                if oj.exists():                       # 留一份上一版，误保存可回退
                    shutil.copy2(oj, oj.with_suffix(".json.bak"))
                oj.write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")
                changes.append(f"大纲：{len(o['pages'])} 页 / 目标 {o.get('minutes')} 分钟")
                st = build_state()
                return self._send(200, json.dumps(st, ensure_ascii=False).encode())
            if g == "narration":
                nj = tdir / "narrations.json"
                narr = json.loads(nj.read_text(encoding="utf-8")) if nj.exists() else {}
                i = int(req["n"])
                txt = (req.get("text") or "").strip()
                old = len(re.sub(r"\s", "", narr.get(str(i), "")))
                narr[str(i)] = txt
                nj.write_text(json.dumps(dict(sorted(narr.items(), key=lambda kv: int(kv[0]))),
                                         ensure_ascii=False, indent=2), encoding="utf-8")
                changes.append(f"解说词第 {i} 页：{old} → {len(re.sub(r'\\s', '', txt))} 字")
                rv = tdir / "review.md"
                if rv.exists():                        # build 以 review.md 为准，必须同步
                    try:
                        update_review(rv, narr, [i])
                        changes.append(f"review.md 第 {i} 页已同步")
                    except Exception as e:
                        print(f"⚠ 同步 review.md 失败：{type(e).__name__}: {e}")
                st = build_state()
                return self._send(200, json.dumps(st, ensure_ascii=False).encode())
            if g == "redo":
                (tdir / "redo.json").write_text(
                    json.dumps(req.get("redo") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
                changes.append(f"重录标记：{len(req.get('redo') or {})} 页")
                return self._send(200, b'{"ok":true}')
            if g == "done":
                try:                                   # 记下"这次通过时这些文件是什么哈希"
                    stage = req.get("tab") or gate or "narration"
                    files = gate_files(deck, out, tdir) if deck else {"narrations": tdir / "narrations.json"}
                    if omd:
                        files["大纲"] = omd
                    gates_approve(out, stage, files)
                    changes.append(f"已记录 {stage} 审核状态（gates.json）")
                except Exception as e:
                    print(f"⚠ 写 gates.json 失败：{type(e).__name__}: {e}")
                log = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "target": str(tgt),
                       "version": version, "changes": changes}
                (work / "review-log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
                self._send(200, json.dumps({"ok": True, "summary": "；".join(changes) or "没有改动"},
                                           ensure_ascii=False).encode())
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._send(400, b'{"error":"unknown gate"}')

    class Srv(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    for p in range(port, port + 20):
        try:
            httpd = Srv((host, p), H)
            port = p
            break
        except OSError:
            continue
    else:
        sys.exit(f"{host}:{port}~{port + 19} 都被占用")
    ips = ["127.0.0.1"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass
    qs = f"?t={TOKEN}" if TOKEN else ""
    urls = [f"http://{i}:{port}/{qs}" for i in dict.fromkeys(ips)]
    print("审核台已启动：")
    for u in urls:
        print("   " + u)
    print(f"   数据：{tgt}\n   完成审核后服务自动关闭。")
    if open_browser and host in ("127.0.0.1", "localhost"):
        try:
            webbrowser.open(urls[0])
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n（手动中断）")
    finally:
        httpd.server_close()
    lg = work / "review-log.json"
    if lg.exists():
        d = json.loads(lg.read_text(encoding="utf-8"))
        print(f"\n审核结束 {d['at']}：")
        for c in d["changes"] or ["（没有改动）"]:
            print("   · " + c)


def main():
    ap = argparse.ArgumentParser(description="幻灯片（HTML / PPTX / PDF）→ 配音讲解视频")
    ap.add_argument("cmd", choices=["check", "outline", "deck-skeleton", "gates", "import", "narrate", "review",
                                    "deck-pdf", "review-doc", "qc", "review-ui", "build"])
    ap.add_argument("deck", nargs="?", help="幻灯片 HTML / PPTX / PDF，或 outline.json")
    ap.add_argument("--out", help="审核产物的输出路径（大纲审定稿 / 预览 PDF / 审定稿 / 质检报告）")
    ap.add_argument("--host", default="127.0.0.1", help="review-ui 绑定地址（默认仅本机，可填 tailnet IP）")
    ap.add_argument("--port", type=int, default=8099, help="review-ui 起始端口（被占用则顺延）")
    ap.add_argument("--gate", choices=["outline", "deck", "narration", "video"],
                    help="review-ui 先打开哪个 tab")
    ap.add_argument("--no-open", action="store_true", help="review-ui 不自动开浏览器")
    ap.add_argument("--reset", action="store_true", help="review 时重新生成 review.md")
    ap.add_argument("--source", help="源文档（.md/.txt/.docx/.pdf…；非文本走 docreader）")
    ap.add_argument("--brief", help="场景卡 md（受众/用途/语气/主线），供解说定调")
    ap.add_argument("--pages", help="只重写指定页，如 3,5,7-9")
    ap.add_argument("--vision", action="store_true",
                    help="把每页截图一并发给模型（需多模态模型；图表/扫描页提取不到文字时用）")
    ap.add_argument("--length",
                    help="单版本模式的每页字数下限-上限（默认 100-180）；生效值写进 review.md，review-doc 与审核台都读它。"
                         "用 --version 时不生效，字数由版本总时长分配")
    ap.add_argument("--version", help="versions.json 里的版本名（如 investor-10）；不传则走单版本流程")
    ap.add_argument("--force", action="store_true",
                    help="忽略审核状态门禁（文件在审核后被改过时仍出片）")
    ap.add_argument("--disclaimer", action="store_true",
                    help="启用免责语。默认关闭：deck 与解说词都不自动出现免责/声明/风险提示内容")
    ap.add_argument("--hash-start", type=int, default=1,
                    help="翻页 hash 起始编号（html-ppt 为 1，reveal.js 为 0；仅 HTML 用）")
    a = ap.parse_args()
    global DISCLAIMER_ON
    if getattr(a, "disclaimer", False):
        DISCLAIMER_ON = True                   # 只有明确要求才启用免责语
    if a.cmd == "check":
        return cmd_check()
    if a.cmd == "outline":                     # Gate 1：outline.json → 大纲审定稿
        if not a.deck:
            ap.error("outline 需要 outline.json 路径")
        return cmd_outline(a.deck, a.out)
    if a.cmd == "deck-skeleton":               # 大纲 → deck 骨架（规范 §六）
        if not a.deck:
            ap.error("deck-skeleton 需要 大纲.md 路径")
        return cmd_deck_skeleton(a.deck, a.out, force=a.force)
    if a.cmd == "review-ui":                   # 审核台：前台阻塞到 owner 点完成
        if not a.deck:
            ap.error("review-ui 需要 outline.json 或 deck 路径")
        return cmd_review_ui(a.deck, a.version, a.host, a.port, a.gate, not a.no_open)
    if a.cmd == "gates" and not a.deck:      # 不给 deck 就报当前目录的状态
        return cmd_gates(Path("."), None)

    if not a.deck:
        ap.error("需要指定幻灯片文件（HTML / PPTX / PDF）")
    deck = str(Path(a.deck).resolve())
    is_html = Path(deck).suffix.lower() in (".html", ".htm")
    # PPTX/PDF 各用一个 <文件名>-video/，同一个目录里放多份幻灯片也不会互相覆盖
    out = out_dir(deck)
    if a.cmd == "gates":
        return cmd_gates(out, deck)
    out.mkdir(exist_ok=True)
    if a.cmd == "import":
        if a.version:
            print("⚠ import 与版本无关，--version 被忽略")
        if is_html:
            return print("HTML 不需要 import（review 时直接截图）")
        return cmd_import(deck, out)
    v = load_version(deck, out, a.version) if a.version else None
    if a.cmd == "deck-pdf":                    # Gate 2：画面预览 PDF
        return cmd_deck_pdf(deck, a.out)
    if a.cmd == "review-doc":                  # Gate 2：解说词审定稿
        return cmd_review_doc(deck, out, v, a.out)
    if a.cmd == "qc":                          # Gate 3：成片质检报告
        return cmd_qc(deck, out, v, a.out)
    try:
        slides = load_slides(deck, out)
    except SystemExit:
        raise
    except Exception as e:                 # 坏 PPTX / 缺 slides.json：给人话，不要 traceback
        sys.exit(f"读不了这份幻灯片（{type(e).__name__}: {str(e)[:80]}）。\n"
                 "  路线 B 请先跑 import；或确认文件没损坏、是幻灯片版式（不是 A4 报告）")
    if not slides:
        sys.exit('没找到幻灯片：HTML 需要 <section class="slide">；PPTX/PDF 请确认是幻灯片版式')
    if a.cmd == "build":
        return cmd_build(out, slides, v, deck=deck, force=a.force)
    if a.cmd == "narrate":
        _ln = a.length
        if not _ln:                              # 命令行没传就读 review.md 里上次生效的值
            _rv = (v["dir"] if v else out) / "review.md"
            if _rv.exists():
                try:
                    _ln = parse_review(_rv)[0].get("length")
                except Exception:
                    _ln = None
        lo, hi = map(int, (_ln or DEFAULTS["length"]).split("-"))
        pgs = parse_pages(a.pages, len(slides)) if a.pages else None
        cmd_narrate(out, slides, v, a.source, pgs, lo, hi, a.brief, a.vision)
    else:
        cmd_review(deck, out, slides, a.reset, a.hash_start, v)


if __name__ == "__main__":
    sys.exit(main())
