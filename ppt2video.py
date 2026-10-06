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

NARRATE_PROMPT = """你是讲解视频的解说撰稿人。下面是一份共 {n} 页幻灯片的文字内容，请为每一页写一段配音解说词。
要求：
- 口语化，像面对面讲解；不要照念幻灯片原文，不要说"本页""如图所示"
- 每页 60-140 字；第 1 页简短开场，最后一页自然收尾
- 页与页之间要有衔接
- 只用中文标点，不要 Markdown、表情、括号注释
- 英文缩写保持原样（如 MCP、API）
只输出一个 JSON 对象，键是页码字符串，值是解说词，例如 {{"1": "...", "2": "..."}}，不要输出其他任何内容。"""


# ─── 幻灯片解析与截图 ─────────────────────────────────────

class SlideParser(HTMLParser):
    """提取每个 <section class="slide"> 的标题和文字。"""
    def __init__(self):
        super().__init__()
        self.slides, self.depth, self.skip = [], 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "section":
            if self.depth == 0 and "slide" in (a.get("class") or "").split():
                self.depth = 1
                self.slides.append({"title": (a.get("data-title") or "").strip(), "text": []})
            elif self.depth:
                self.depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag == "section" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip and data.strip():
            self.slides[-1]["text"].append(data.strip())


def extract(deck):
    p = SlideParser()
    p.feed(Path(deck).read_text(encoding="utf-8"))
    return p.slides


def write_outline(out, slides):
    md = "\n\n".join(f"## 第{i}页 {s['title']}\n" + "\n".join(s["text"])
                     for i, s in enumerate(slides, 1))
    Path(out, "outline.md").write_text(md, encoding="utf-8")
    return md


def render(deck, out, n, start):
    if not CHROME:
        sys.exit("找不到浏览器（Edge / Chrome / Chromium 都没有）。可用环境变量 BROWSER 指定。")
    print(f"  浏览器: {CHROME}")
    d = Path(out, "slides")
    d.mkdir(parents=True, exist_ok=True)
    uri = Path(deck).as_uri()
    for i in range(1, n + 1):
        png = d / f"{i:03d}.png"                      # 3 位，避免 >99 页排序错乱
        # Edge/Chrome 无头截图**经常截完不退出**（实测：M2 的 Edge 154 会挂住），
        # 必须给每次截图加硬超时并杀掉，否则整个 review 卡死。
        for attempt in range(3):
            png.unlink(missing_ok=True)
            proc = subprocess.Popen(
                [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--no-sandbox", "--virtual-time-budget=4000", "--window-size=1920,1080",
                 f"--screenshot={png}", f"{uri}#/{i - 1 + start}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.wait(timeout=25)                 # 正常 2~5 秒出图
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            if png.exists() and png.stat().st_size > 0:
                break
            print(f"    ↻ 第 {i} 页截图超时/为空，重试 {attempt + 1}/3")
        if not png.exists() or png.stat().st_size == 0:
            sys.exit(f"第 {i} 页截图失败（检查浏览器能否用 #/N 翻页，reveal.js 试 --hash-start 0）")
        print(f"  ✔ slides/{png.name}")


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
            self.key = ["qwen-clone", cfg["qwen_model"], self.ref_audio, self.ref_text,
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


def cmd_narrate(out, slides):
    base = (os.environ.get("LLM_BASE_URL") or DEFAULT_LLM_BASE).rstrip("/")
    model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
    body = {"model": model, "temperature": 0.7, "messages": [
        {"role": "system", "content": NARRATE_PROMPT.format(n=len(slides))},
        {"role": "user", "content": write_outline(out, slides)}]}
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'none')}"})
    print(f"请求 {base} 的 {model} 生成 {len(slides)} 页解说词…")
    with urllib.request.urlopen(req, timeout=900) as r:
        text = json.load(r)["choices"][0]["message"]["content"] or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    try:
        narr = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except json.JSONDecodeError:
        Path(out, "narrate-raw.txt").write_text(text, encoding="utf-8")
        sys.exit("模型输出不是合法 JSON，原文已存到 narrate-raw.txt")
    narr = {str(k): str(v).strip() for k, v in narr.items()}
    Path(out, "narrations.json").write_text(json.dumps(narr, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    miss = [i for i in range(1, len(slides) + 1) if not narr.get(str(i))]
    print("已写入 narrations.json" + (f"，缺少第 {miss} 页" if miss else ""))
    if Path(out, "review.md").exists():
        print("注意：review.md 已存在，要用新解说词请运行 review --reset")


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


def mux(out, mode, total):
    if mode == "burn" and not has_libass():
        print("⚠ 当前 ffmpeg 不支持 subtitles 滤镜，改为软字幕")
        mode = "soft"
    vf = ("fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
          "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
    if mode == "burn":
        vf += (",subtitles=subs.srt:force_style='FontName=PingFang SC,FontSize=15,"
               "Outline=1.2,Shadow=0,MarginV=24'")
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


def cmd_build(out):
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
    tts = QwenTTS(cfg) if cfg["backend"] == "qwen" else EdgeTTS(cfg)
    print(f"配音模式: {'克隆 ' + tts.ref_audio if getattr(tts, 'clone', False) else '预置音色 ' + cfg['speaker']}")
    cache = Path(out, "tts-cache"); cache.mkdir(exist_ok=True)
    silence = lambda s: np.zeros(int(round(s * SR)), dtype=np.int16)

    track, cues, ffc, t = [], [], ["ffconcat version 1.0"], 0.0
    for num, text in pages:
        parts, cur = [silence(LEAD)], LEAD
        seg_list = merge_short(sentences(text))
        for s, wavp in zip(seg_list, cached_many(tts, seg_list, cache)):
            a = read_wav_sped(wavp, float(cfg.get("speed", 1.0) or 1.0), cache)
            d = len(a) / SR
            cues += split_cue(s, t + cur, t + cur + d)
            parts += [a, silence(GAP)]
            cur += d + GAP
        parts.append(silence(TAIL))
        seg = np.concatenate(parts)
        dur = len(seg) / SR
        track.append(seg)
        ffc += [f"file 'slides/{num:03d}.png'", f"duration {dur:.6f}"]
        t += dur
        print(f"  ✔ 第 {num:2d} 页  {dur:5.1f}s")
    ffc.append(f"file 'slides/{pages[-1][0]:03d}.png'")   # concat 要求最后一帧重复一次

    write_wav(Path(out, "narration.wav"), np.concatenate(track))
    Path(out, "list.ffconcat").write_text("\n".join(ffc) + "\n", encoding="utf-8")
    Path(out, "subs.srt").write_text(
        "".join(f"{i}\n{ts(a)} --> {ts(b)}\n{c}\n\n" for i, (a, b, c) in enumerate(cues, 1)),
        encoding="utf-8")
    mux(out, cfg["subtitles"], t)


def main():
    ap = argparse.ArgumentParser(description="HTML 幻灯片 → 配音讲解视频")
    ap.add_argument("cmd", choices=["check", "narrate", "review", "build"])
    ap.add_argument("deck", nargs="?", help="幻灯片 HTML 文件")
    ap.add_argument("--reset", action="store_true", help="review 时重新生成 review.md")
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
    if a.cmd == "build":
        return cmd_build(out)
    slides = extract(deck)
    if not slides:
        sys.exit('没找到 <section class="slide">')
    if a.cmd == "narrate":
        cmd_narrate(out, slides)
    else:
        cmd_review(deck, out, slides, a.reset, a.hash_start)


if __name__ == "__main__":
    main()
