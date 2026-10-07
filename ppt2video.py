#!/usr/bin/env python3
"""ppt2video.py — HTML 幻灯片 → 配音讲解视频（M2 本地实况适配版）

适配的实际情况（2026-10-06 实测）：
  · M2 没装 Chrome，但装了 Microsoft Edge（Chromium 内核，--headless 参数通用）
  · M2 没装 brew，但 pip 的 imageio-ffmpeg 带静态 ffmpeg 7.1 且**支持 libass**（可烧录字幕）
  · mlx-audio 的克隆接口是 Model.generate(ref_audio=, ref_text=)，**没有 generate_voice_clone**
  · HF 直连被墙 → 模型必须用**本地绝对路径**，不能用 HF 仓库 ID
  · 不设 repetition_penalty 会翻车（实测 44 字念出 32 秒）

用法：
  python ppt2video.py check
  python ppt2video.py narrate deck/index.html     # 调 DGX infersight 写解说词
  python ppt2video.py review  deck/index.html     # 截图 + 生成 review.md
  python ppt2video.py build   deck/index.html     # 配音 + 字幕 + 合成视频
"""
import argparse, asyncio, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, wave
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import numpy as np

SR = 24000                        # 统一采样率
LEAD, GAP, TAIL = 0.3, 0.25, 0.6  # 每页开头留白 / 句间停顿 / 每页结尾留白（秒）
SUB_MAX = 24                      # 每行字幕最多字数
PLACEHOLDER = "（在这里写解说词）"

# 时长合理性区间（字/秒）：实测中文解说约 3.2 字/秒 → 0.31 s/字
#   下界 0.20 → 最快 5 字/秒；上界 0.45 → 最慢 2.2 字/秒。超出即判定异常。
SEC_PER_CHAR_LO, SEC_PER_CHAR_HI = 0.16, 0.50
CPS = 4.5   # 字/秒：每页目标字数 = data-sec × CPS，首次 build 后按实测校准
DISCLAIMER = "以上财务数据为正向情景测算，不构成业绩或收益承诺，实际结果以正式披露为准。"
DISC_WORDS = r"不构成.{0,6}承诺|情景测算|以正式披露为准"
OPENERS = ["首先", "其次", "最后", "那么"]   # 只查句首（避免误伤"最后一公里""最后阶段"）
# 分类禁用词：键是类别（用于报错），值是词表。按子串匹配。
BANNED = {
    "对话与呼语": ["大家", "你会", "你看", "咱们", "我们来看", "好，", "注意，", "说白了", "说穿了"],
    "标签式套话": ["一句话", "八个字", "核心就", "结论很直接", "先说结论", "简单说", "综上所述", "值得注意的是"],
    "画面指代": ["上一页", "下一页", "这一页", "本页", "这张表", "如图", "左边", "右边", "看到的"],
    "口语俚语": ["干到", "手里的牌", "绑得很死", "难啃", "差远了", "一条龙", "拉远看", "根子", "把椅子"],
    "绝对化": ["绝对", "一层不落", "没有被撼动", "毫无疑问", "遥遥领先", "无可替代", "极强"],
}
MIN_SYNTH_CHARS = 0            # 【已停用】曾设 12 想把短句并入下一句以救音色，
                              # 但实测用户判断更差：停顿被吃掉（"大家好"与下句连读）。声纹相似度

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

# ─── 默认配置 ────────────────────────────────────────────
# 注意：qwen_model 必须是**本地绝对路径**——用 HF 仓库 ID 会触发联网下载，而 HF 被墙。
DEFAULTS = {
    "backend": "qwen",            # qwen / edge
    "qwen_model": "/Users/<user>/models/<qwen-tts-8bit>",
    # —— 克隆模式（backend=qwen 且填了 ref_audio 时启用）——
    "ref_audio": "/Users/<user>/voice.wav",
    "ref_text": "嗯，大家好，嗯，我是<姓名>，我是广东某 OLED 材料公司光电材料有限公司的负责人。今天由我向大家介绍一下我们公司的一些基本情况。嗯，如果有问题，请随时提问，谢谢。",
    # —— 预置音色模式（ref_audio 为空时启用）——
    "speaker": "Vivian",          # Vivian / Serena / Uncle_Fu / Dylan / Eric
    "instruct": "",
    # —— edge-tts 备用 ——
    "edge_voice": "zh-CN-YunxiNeural",
    "edge_rate": "+5%",
    "subtitles": "burn",          # burn（烧录）/ soft（软字幕）/ off
    "speed": 1.15,                # 变速（ffmpeg atempo，不变调）。模型自带的 speed 参数在克隆模式下无效，只能后处理
}

# narrate 默认走 DGX 上的 infersight 网关
DEFAULT_LLM_BASE = "http://100.89.119.47:9000/v1"
DEFAULT_LLM_MODEL = "main"

SOURCE_MAX = int(os.environ.get("SOURCE_MAX_CHARS", "60000"))

# 机队共享的文档解析服务（M2）。事实源 ~/router/docs/docreader-m2.md
DOCREADER = os.environ.get("DOCREADER_URL", "http://100.89.60.63:50052")

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
                                    "sec": int(a["data-sec"]) if (a.get("data-sec") or "").isdigit() else None,
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
        b.close()


# ─── review.md ───────────────────────────────────────────

def write_review(path, slides, narr, cfg):
    lines = ["---", *[f"{k}: {v}" for k, v in cfg.items()], "---", "",
             "<!-- 改代码块里的解说词；[ ] 改成 [x] 跳过该页；",
             "     backend: qwen / edge ；subtitles: burn / soft / off",
             "     ref_audio 填本地录音路径 = 克隆你的声音；清空则用 speaker 预置音色 -->", ""]
    for i, s in enumerate(slides, 1):
        title = s["title"] or (s["text"][0][:20] if s["text"] else f"Slide {i}")
        lines += [f"# {i} · {title}", "", "- [ ] 跳过此页", "", f"![](slides/{i:03d}.png)", "",
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
            # 克隆路径也把 instruct 传下去（_generate_icl 签名里没有它，
            # 但 generate() 会按是否给了 instruct 选择内部路径 —— 实测才知道生不生效）
            if cfg.get("instruct"):
                self.kw["instruct"] = cfg["instruct"]
            # 缓存键带参考音频**内容**哈希：换参考音频（哪怕同名覆盖）才会重建缓存，
            # 否则旧音频的缓存会被错误复用。
            self.key = ["qwen-clone", cfg["qwen_model"], self.ref_audio,
                        _file_hash(self.ref_audio), self.ref_text,
                        cfg.get("instruct", "")]
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


def cached_many(tts, texts, cache):
    """返回 texts 对应的 wav 路径列表；未命中的句子**一次 batch** 合成。"""
    paths, miss = [], []
    for t in texts:
        h = hashlib.sha1(json.dumps(tts.key + [t], ensure_ascii=False).encode()).hexdigest()[:16]
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
                tts.synth(t, p)
        else:
            for (t, p), a in zip(miss, audios):
                write_wav(p, (np.clip(a, -1, 1) * 32767).astype(np.int16), tts.sr)
    return paths


def cached(tts, text, cache):
    h = hashlib.sha1(json.dumps(tts.key + [text], ensure_ascii=False).encode()).hexdigest()[:16]
    p = cache / f"{h}.wav"
    if not p.exists():
        tts.synth(text, p)
    return p


# ─── 字幕 ────────────────────────────────────────────────

def sentences(text):
    text = re.sub(r"\s+", " ", text.strip())
    return [s.strip() for s in re.findall(r"[^。！？；!?;…]+[。！？；!?;…]*", text) if s.strip()]


def merge_short(segs, min_chars=MIN_SYNTH_CHARS):
    """把过短的句子并到相邻句再合成 —— 克隆模型在极短文本上抓不住音色。

    实测（2026-10-06，同一参考音频）：
        大家好。        4 字 → 声纹相似度 0.9199，且只出 0.56s 音频（提前截断）
        22 字长句           → 相似度 0.9838
    合并后仍由 split_cue 按字数比例切回字幕，句级时间轴不受影响。
    """
    out, buf = [], ""
    for seg in segs:
        buf += seg
        if len(buf) >= min_chars:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] += buf          # 尾句太短 → 并入上一句
        else:
            out.append(buf)
    return out


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
# 单位/范围 → 读法（先于 cn2an，避免 cn2an 把 "ms" 当字母读）。
# 用 (?![A-Za-z]) 而非 \b：数字后面常跟中文（"120ms降"），\b 在 CJK 前不成立。
_SPEAK_RULES = [
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
        print(f"    ⚠ cn2an 转换失败（{type(e).__name__}），用原文：{text[:20]}…")
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
    row(CHROME is not None, "浏览器（Edge/Chrome）", "装 Edge 或用 BROWSER 指定")
    if CHROME:
        print(f"      {CHROME}")
    row(importlib.util.find_spec("mlx_audio") is not None, "mlx-audio", "pip install mlx-audio", False)
    row(importlib.util.find_spec("edge_tts") is not None, "edge-tts", "pip install edge-tts", False)
    row(importlib.util.find_spec("numpy") is not None, "numpy", "pip install numpy")
    # ★ 新增：本地模型存在性（HF ID 会触发下载 → 被墙，这是最容易踩的坑）
    for tag, p in (("克隆模型", DEFAULTS["qwen_model"]), ("参考音频", DEFAULTS["ref_audio"])):
        exists = os.path.exists(p)
        row(exists, f"{tag}（本地）", p, required=False)
    print("必需依赖齐全" if ok else "缺少必需依赖")


def load_source(path):
    """读源文档。docx/xlsx/pptx/pdf/epub 走机队共享的 docreader（M2 :50052）。"""
    import urllib.parse
    p = Path(path)
    if not p.exists():
        sys.exit(f"源文档不存在：{path}")
    if p.suffix.lower() in (".md", ".markdown", ".txt"):
        text = p.read_text(encoding="utf-8")
    else:
        ext = p.suffix.lower().lstrip(".")
        url = (f"{DOCREADER}/read?file_name={urllib.parse.quote(p.name)}"
               f"&file_type={urllib.parse.quote(ext)}")
        req = urllib.request.Request(url, data=p.read_bytes(),
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                res = json.load(r)
        except Exception as e:
            sys.exit(f"docreader 调用失败（{DOCREADER}）：{type(e).__name__}: {e}")
        if not res.get("ok"):            # ⚠️ docreader 失败是 HTTP 200 + ok:false
            sys.exit(f"docreader 解析失败：{str(res)[:300]}")
        text = res.get("markdown") or ""
        print(f"  docreader: {len(text)} 字符 / {res.get('image_count', 0)} 张图 / "
              f"{res.get('elapsed_ms', '?')} ms / 引擎 {res.get('metadata', {}).get('engine', '?')}")
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


def llm(system, user):
    base = (os.environ.get("LLM_BASE_URL") or DEFAULT_LLM_BASE).rstrip("/")
    model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
    body = {"model": model, "temperature": 0.5,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'none')}"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        text = json.load(r)["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


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


def page_floor(sec, lo):
    """有 data-sec 时按 时长×CPS×0.8 推字数下限，否则用全局 lo。只设下限不设上限。"""
    if sec:
        return max(lo, int(sec * CPS * 0.8))
    return lo


def ungrounded_numbers(text, grounded):
    """text 里的数字若不在 grounded（画面+讲述要点+源文档段落+上一页解说）中，视为可疑（防编造数字）。
    逗号归一化（1,000 与 1000 视为同一数，避免误报）；跳过单位数整数（常为范围/序数，噪声大）。"""
    nums = re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
    g = set(re.findall(r"\d+(?:\.\d+)?", grounded.replace(",", "")))
    return sorted({n for n in nums if (len(n) > 1 or "." in n) and n not in g})


def lint(text, lo, grounded=None, disc=False):
    """检查字数下限、问句、套话、画面指代、俚语、绝对化、句子过碎、禁用符号、免责页、数字溯源。
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
    if disc:
        if not re.search(DISC_WORDS, text):
            issues.append("缺少免责声明")
    elif re.search(DISC_WORDS, text):
        issues.append("免责语只出现在指定页")
    if grounded is not None:
        un = ungrounded_numbers(text, grounded)
        if un:
            issues.append("数字无出处：" + "、".join(un))
    return issues


def cmd_narrate(out, slides, source=None, pages=None, mode="auto", lo=100, hi=180, brief=None):
    """逐页生成解说词。每页独立调用 LLM（system 带本页免责口径），不再整篇一次生成——
    整篇模式会让模型套用"承接→结论→引出"的固定结构，写出电报体碎句。"""
    n, outline = len(slides), write_outline(out, slides)
    nj, rv = Path(out, "narrations.json"), Path(out, "review.md")
    narr = json.loads(nj.read_text(encoding="utf-8")) if nj.exists() else {}
    src = load_source(source) if source else ""
    if src:
        Path(out, "source.md").write_text(src, encoding="utf-8")   # 方便检查 docx 转换效果
    brief = (Path(brief).read_text(encoding="utf-8").strip() if brief else "")
    if brief:
        Path(out, "brief.md").write_text(brief, encoding="utf-8")
    save = lambda: nj.write_text(json.dumps(dict(sorted(narr.items(), key=lambda kv: int(kv[0]))),
                                            ensure_ascii=False, indent=2), encoding="utf-8")
    chunks = chunk_source(src) if src else []
    title = lambda t: t["title"] or (t["text"][0][:20] if t["text"] else "")
    targets = pages or range(1, n + 1)
    print(f"逐页模式：源文档切成 {len(chunks)} 块，生成 {len(targets)} 页…")
    for i in targets:
        pg = slides[i - 1]
        floor = page_floor(pg["sec"], lo)
        page = "\n".join([pg["title"], *pg["text"]]).strip()
        notes = "\n".join(pg["notes"]) if pg.get("notes") else "（无）"
        src_seg = relevant(chunks, page) if chunks else ""
        prev = narr.get(str(i - 1), "")
        grounded = "\n".join([brief, page, notes, src_seg, prev])   # 数字溯源依据（含口径表）
        disc = pg.get("disc", False)
        system = NARRATE_SYSTEM.format(
            disclaimer=DISCLAIMER if disc else "本页不涉及财务预测，不要说免责语。",
            brief=brief or "（无场景卡）")
        toc = "\n".join(f"{j}. {'**' + title(t) + '**（本页）' if j == i else title(t)}"
                        for j, t in enumerate(slides, 1))
        user = (f"【整套幻灯片目录】\n{toc}\n\n" +
                f"【上一页的解说】\n{prev or ('（这是第一页）' if i == 1 else '（无）')}\n\n" +
                f"【当前页·画面】\n{page}\n\n" +
                f"【当前页·讲述要点】\n{notes}\n\n" +
                f"【源文档相关段落】\n{src_seg or '（无）'}")
        text = llm(system, user).strip().strip('"“”')
        for attempt in range(2):
            iss = lint(text, floor, grounded, disc=disc)
            if not iss:
                break
            print(f"    ⚠ 第 {i} 页 {iss} → 重写 {attempt + 1}/2")
            rewrite = llm(system, f"【当前页·画面】\n{page}\n\n【当前页·讲述要点】\n{notes}\n\n"
                                   f"【本页解说·草稿】\n{text}\n\n【问题】\n" + "\n".join(iss) +
                                   "\n\n请重写这一页解说词，只输出正文。").strip().strip('"“”')
            if rewrite:
                text = rewrite
        narr[str(i)] = text
        save()                                                    # 每页都存，中断不丢进度
        print(f"  ✔ 第 {i:2d} 页  {len(text)} 字（下限 {floor}）")

    miss = [i for i in range(1, n + 1) if not narr.get(str(i))]
    print("已写入 narrations.json" + (f"，缺少第 {miss} 页" if miss else ""))
    if rv.exists():
        if pages:
            update_review(rv, narr, pages)
            print(f"已同步 review.md 中第 {list(pages)} 页，其他页保持不变")
        else:
            # 全量重生成：把 review.md 的解说词同步为新 narrations.json（保留原配置头），
            # 避免 build 读到旧解说词（build 以 review.md 为准）。
            cfg, _ = parse_review(rv)
            write_review(rv, slides, narr, cfg)
            print("已同步 review.md 为新解说词（保留原配置），可直接 build")


def cmd_review(deck, out, slides, reset, start):
    write_outline(out, slides)
    render(deck, out, len(slides), start)
    rv = Path(out, "review.md")
    if rv.exists() and not reset:
        print("已重新截图，保留现有 review.md（加 --reset 可重新生成）")
        return
    nj = Path(out, "narrations.json")
    narr = json.loads(nj.read_text(encoding="utf-8")) if nj.exists() else {}
    write_review(rv, slides, narr, DEFAULTS)
    print(f"已生成 {rv}，修改确认后运行 build")


def mux(out, mode, total, fontdir=None):
    if mode == "burn" and not has_libass():
        print("⚠ 当前 ffmpeg 不支持 subtitles 滤镜，改为软字幕")
        mode = "soft"
    vf = ("fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
          "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
    if mode == "burn":
        style = "FontName=Noto Sans SC,FontSize=15,Outline=1.2,Shadow=0,MarginV=24"
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
            "-t", f"{total:.3f}", "final-video.mp4"]
    subprocess.run(cmd, cwd=out, check=True)
    print(f"\n完成：{Path(out, 'final-video.mp4')}（{total / 60:.1f} 分钟）")


def cmd_build(out, slides):
    rv = Path(out, "review.md")
    if not rv.exists():
        sys.exit("先运行 review")
    cfg, pages = parse_review(rv)
    pages = [(n, t) for n, skip, t in pages if not skip]
    bad = [n for n, t in pages if not t or t == PLACEHOLDER]
    if bad:
        sys.exit(f"这些页还没有解说词：{bad}")
    if not pages:
        sys.exit("所有页面都被跳过了")
    # 读音表：优先用本片 video-output/pronounce.json（专有名词/化学元素），覆盖全局
    pf = Path(out, "pronounce.json")
    if pf.exists():
        PRON.update(json.loads(pf.read_text(encoding="utf-8")))
    tts = QwenTTS(cfg) if cfg["backend"] == "qwen" else EdgeTTS(cfg)
    print(f"配音模式: {'克隆 ' + tts.ref_audio if getattr(tts, 'clone', False) else '预置音色 ' + cfg['speaker']}")
    cache = Path(out, "tts-cache"); cache.mkdir(exist_ok=True)
    silence = lambda s: np.zeros(int(round(s * SR)), dtype=np.int16)

    track, cues, ffc, t = [], [], ["ffconcat version 1.0"], 0.0
    speech, chars = 0.0, 0                                          # 实测语速累计
    for num, text in pages:
        parts, cur = [silence(LEAD)], LEAD
        seg_list = merge_short(sentences(text))
        spoken = [speak(s) for s in seg_list]          # TTS 读口语；字幕仍用书面 s
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
        ffc += [f"file 'slides/{num:03d}.png'", f"duration {dur:.6f}"]
        t += dur
        sec = slides[num - 1].get("sec")
        # 内容优先：比目标短（内容被砍）才告警；比目标长是信息量充足，正常。
        warn = sec and dur / float(sec) < 0.8
        print(f"  ✔ 第 {num:2d} 页  {dur:5.1f}s" + (f" / 目标 {sec}s" if sec else "")
              + ("  ⚠ 偏短，可能漏内容" if warn else ""))
    ffc.append(f"file 'slides/{pages[-1][0]:03d}.png'")   # concat 要求最后一帧重复一次
    if speech:
        print(f"实测语速 {chars / speech:.1f} 字/秒，可设 CPS={chars / speech:.1f}")

    write_wav(Path(out, "narration.wav"), np.concatenate(track))
    Path(out, "list.ffconcat").write_text("\n".join(ffc) + "\n", encoding="utf-8")
    Path(out, "subs.srt").write_text(
        "".join(f"{i}\n{ts(a)} --> {ts(b)}\n{c}\n\n" for i, (a, b, c) in enumerate(cues, 1)),
        encoding="utf-8")
    fontdir = Path(out).parent / "subtitles" / "fonts"
    mux(out, cfg["subtitles"], t, str(fontdir) if fontdir.is_dir() else None)


def main():
    ap = argparse.ArgumentParser(description="HTML 幻灯片 → 配音讲解视频")
    ap.add_argument("cmd", choices=["check", "narrate", "review", "build"])
    ap.add_argument("deck", nargs="?", help="幻灯片 HTML 文件")
    ap.add_argument("--reset", action="store_true", help="review 时重新生成 review.md")
    ap.add_argument("--source", help="源文档（.md/.txt/.docx/.pdf…；非文本走 docreader）")
    ap.add_argument("--brief", help="场景卡 md（受众/用途/语气/主线），供解说定调")
    ap.add_argument("--pages", help="只重写指定页，如 3,5,7-9")
    ap.add_argument("--mode", choices=["auto", "full", "page"], default="page")
    ap.add_argument("--length", default="100-180",
                    help="无 data-sec 的页用此全局字数范围（有 data-sec 时按 时长×CPS 推算）")
    ap.add_argument("--hash-start", type=int, default=1,
                    help="翻页 hash 起始编号（html-ppt 为 1，reveal.js 为 0）")
    a = ap.parse_args()
    if a.cmd == "check":
        return cmd_check()
    if not a.deck:
        ap.error("需要指定幻灯片 HTML 文件")
    deck = str(Path(a.deck).resolve())
    out = Path(deck).parent / "video-output"
    out.mkdir(exist_ok=True)
    slides = extract(deck)
    if not slides:
        sys.exit('没找到 <section class="slide">')
    if a.cmd == "build":
        return cmd_build(out, slides)
    if a.cmd == "narrate":
        lo, hi = map(int, a.length.split("-"))
        pgs = parse_pages(a.pages, len(slides)) if a.pages else None
        cmd_narrate(out, slides, a.source, pgs, a.mode, lo, hi, a.brief)
    else:
        cmd_review(deck, out, slides, a.reset, a.hash_start)


if __name__ == "__main__":
    main()
