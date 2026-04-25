#!/usr/bin/env python3
"""build_video.py — HTML PPT 转讲解视频

Usage:
    python build_video.py review              # 生成 review.md 确认文档
    python build_video.py build               # 解析 review.md 并生成视频
    python build_video.py build --step 3      # 仅执行 build 步骤 3
    python build_video.py check               # 仅检查依赖
"""

import os
import re
import subprocess
import sys
import glob

# ============================================================
# ⬇️ 每次使用前修改这些常量 ⬇️
# ============================================================

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "video-output")
DECK = os.path.join(BASE, "deck", "index.html")      # HTML PPT 文件路径
RENDER_SH = os.path.expanduser("~/.claude/skills/html-ppt/scripts/render.sh")
N = 14                                                # 幻灯片页数

# 默认参数（会写入 review.md，用户可在确认文档中修改）
DEFAULT_VOICE = "zh-CN-YunxiNeural"                   # TTS 语音（见 SKILL.md Voice Options）
DEFAULT_RATE = "+5%"                                  # 语速调整
DEFAULT_STYLE = "口语化"

# 每页解说词（口语化，80-140 字/页）
NARRATIONS = {
    1: "在这里写第一页的解说词。",
    2: "在这里写第二页的解说词。",
    # ... 按需添加
}

# ============================================================
# ⬆️ 修改到此结束 ⬆️
# ============================================================

ASS_STYLE = ("Style: Default,Noto Sans SC,12,&H00FFFFFF,&H000000FF,"
             "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,2,"
             "6,6,6,1")


def run(cmd, **kw):
    print(f"  $ {cmd}")
    subprocess.run(cmd, shell=True, check=True, **kw)


def ensure_dirs():
    for d in ["narrations", "audio", "slides", "segments", "subtitles"]:
        os.makedirs(os.path.join(OUT, d), exist_ok=True)


# ─── Dependency Check ──────────────────────────────────────

def check_deps():
    """Check all required dependencies before running the pipeline."""
    errors = []

    try:
        subprocess.run(["edge-tts", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        errors.append("edge-tts not found. Install: pip install edge-tts")

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        errors.append("ffmpeg not found. Install: brew install ffmpeg")

    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        errors.append("ffprobe not found. Install: brew install ffmpeg")

    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not os.path.exists(chrome_path):
        errors.append(f"Chrome not found at {chrome_path}")

    if not os.path.exists(RENDER_SH):
        errors.append(f"render.sh not found at {RENDER_SH}. "
                      f"Install html-ppt skill: npx skills add https://github.com/lewislulu/html-ppt-skill -y -g")

    if not os.path.exists(DECK):
        errors.append(f"Deck HTML not found: {DECK}")

    noto = glob.glob(os.path.expanduser("~/Library/Fonts/NotoSans*SC*"))
    st_heiti = os.path.exists("/System/Library/Fonts/STHeiti Medium.ttc")
    if not noto and not st_heiti:
        errors.append("Noto Sans SC font not found. Install: brew install font-noto-sans-sc")

    if errors:
        print("依赖检查失败：")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("依赖检查通过")


# ─── Review Document ───────────────────────────────────────

def get_slide_titles():
    """Extract slide titles from HTML deck."""
    titles = {}
    try:
        with open(DECK) as f:
            content = f.read()
        slides = re.findall(r'<section[^>]*data-title="([^"]*)"', content)
        for i, title in enumerate(slides, 1):
            titles[i] = title
    except Exception:
        pass
    return titles


def render_slides():
    """Render HTML slides to PNG images."""
    slides_dir = os.path.join(OUT, "slides")
    run(f"bash {RENDER_SH} {DECK} {N} {slides_dir}")
    print(f"幻灯片已渲染 ({N} 个 PNG)")


def generate_review():
    """Generate review.md with checkboxes, slides, narrations, and YAML config."""
    ensure_dirs()

    # Render slides first
    render_slides()

    titles = get_slide_titles()
    review_path = os.path.join(OUT, "review.md")

    lines = [
        "---",
        f"voice: {DEFAULT_VOICE}",
        f"rate: {DEFAULT_RATE}",
        f"style: {DEFAULT_STYLE}",
        "skip: []",
        "---",
        "",
        "<!--",
        "说明：",
        "  - 每个 `# N · Title` 下有一个 `- [ ]` checkbox",
        "  - 取消勾选改为 `- [x]` 表示跳过该页（不生成语音和视频）",
        "  - 解说词在 code block 中，可直接编辑",
        "  - YAML 块中可修改 voice、rate、style 等参数",
        "  - 确认无误后告知继续",
        "-->",
        "",
    ]

    for i in range(1, N + 1):
        title = titles.get(i, f"Slide {i}")
        img = os.path.join("slides", f"index_{i:02d}.png")
        narration = NARRATIONS.get(i, "")

        lines.append(f"# {i} · {title}")
        lines.append("")
        lines.append(f"- [ ] **跳过此页**")
        lines.append("")
        if os.path.exists(os.path.join(OUT, img)):
            lines.append(f"![slide]({img})")
            lines.append("")
        lines.append("```")
        lines.append(narration)
        lines.append("```")
        lines.append("")

    with open(review_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\n确认文档已生成: {review_path}")
    print("请打开检查，修改解说词或参数后告知继续。")


# ─── Parse Review ──────────────────────────────────────────

def parse_review():
    """Parse review.md to extract config and per-slide narrations."""
    review_path = os.path.join(OUT, "review.md")
    if not os.path.exists(review_path):
        print(f"错误: 找不到 {review_path}，请先运行 review 命令")
        sys.exit(1)

    with open(review_path) as f:
        content = f.read()

    # Parse YAML frontmatter
    config = {
        "voice": DEFAULT_VOICE,
        "rate": DEFAULT_RATE,
        "style": DEFAULT_STYLE,
        "skip": [],
    }
    yaml_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if yaml_match:
        for line in yaml_match.group(1).strip().split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                key = key.strip()
                val = val.strip()
                if key == "skip":
                    config["skip"] = [int(x.strip()) for x in val.strip("[]").split(",") if x.strip().isdigit()]
                else:
                    config[key] = val

    # Parse slides
    slides = {}
    # Split by slide headers: # N · Title
    sections = re.split(r'^# \d+ · .+$', content, flags=re.MULTILINE)
    headers = re.findall(r'^# (\d+) · (.+)$', content, flags=re.MULTILINE)

    for idx, (num_str, title) in enumerate(headers):
        num = int(num_str)
        body = sections[idx + 1] if idx + 1 < len(sections) else ""

        # Check skip: checkbox `- [x]` or in YAML skip list
        skipped = bool(re.search(r'-\s*\[x\]', body, re.IGNORECASE))
        if not skipped and num in config.get("skip", []):
            skipped = True

        # Extract narration from code block
        code_match = re.search(r'```\s*\n(.*?)```', body, re.DOTALL)
        if code_match:
            narration = code_match.group(1).strip()
        else:
            # Fallback: extract non-image, non-checkbox, non-comment lines
            narration_lines = []
            for line in body.strip().split('\n'):
                line = line.strip()
                if line.startswith("![") or line.startswith("<!--") or line.startswith("- ["):
                    continue
                if line:
                    narration_lines.append(line)
            narration = " ".join(narration_lines).strip()

        slides[num] = {
            "title": title,
            "narration": narration,
            "skip": skipped,
        }

    return config, slides


# ─── Build Pipeline ────────────────────────────────────────

def get_duration(mp3_path):
    """Get duration of an mp3 file in seconds."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", mp3_path],
        capture_output=True, text=True, check=True
    )
    return float(r.stdout.strip())


def write_narrations(slides):
    """Write narration txt files from parsed review."""
    d = os.path.join(OUT, "narrations")
    count = 0
    for num, slide in slides.items():
        if slide["skip"]:
            continue
        path = os.path.join(d, f"slide_{num:02d}.txt")
        with open(path, "w") as f:
            f.write(slide["narration"])
        count += 1
    print(f"[1/5] 解说词已写入 ({count} 个文件)")


def generate_audio(config, slides):
    """edge-tts → mp3 + srt for each non-skipped slide."""
    voice = config.get("voice", DEFAULT_VOICE)
    rate = config.get("rate", DEFAULT_RATE)
    d = os.path.join(OUT, "audio")
    count = 0
    for num, slide in slides.items():
        if slide["skip"]:
            continue
        txt = os.path.join(OUT, "narrations", f"slide_{num:02d}.txt")
        mp3 = os.path.join(d, f"slide_{num:02d}.mp3")
        srt = os.path.join(d, f"slide_{num:02d}.srt")
        run(f'edge-tts --voice {voice} --rate "{rate}" '
            f"--file {txt} --write-media {mp3} --write-subtitles {srt}")
        count += 1
    print(f"[2/5] 语音已生成 ({count} 个 mp3 + srt)")


def build_combined_srt(slides):
    """Merge per-slide SRTs with cumulative time offset."""
    ts_re = re.compile(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})')

    def shift_ts(match, offset_ms):
        h, m, s, ms = (int(match.group(1)), int(match.group(2)),
                        int(match.group(3)), int(match.group(4)))
        total = h * 3600000 + m * 60000 + s * 1000 + ms + int(offset_ms)
        nh = total // 3600000
        nm = (total % 3600000) // 60000
        ns = (total % 60000) // 1000
        nms = total % 1000
        return f"{nh:02d}:{nm:02d}:{ns:02d},{nms:03d}"

    entries = []
    idx = 1
    offset_ms = 0.0
    total_dur = 0.0

    for num in sorted(slides.keys()):
        if slides[num]["skip"]:
            continue
        srt_path = os.path.join(OUT, "audio", f"slide_{num:02d}.srt")
        mp3_path = os.path.join(OUT, "audio", f"slide_{num:02d}.mp3")
        if not os.path.exists(srt_path):
            continue

        with open(srt_path) as f:
            content = f.read()
        content = ts_re.sub(lambda m: shift_ts(m, offset_ms), content)
        blocks = re.split(r'\n\n+', content.strip())
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                entries.append(f"{idx}\n" + "\n".join(lines[1:]))
                idx += 1

        dur = get_duration(mp3_path)
        offset_ms += dur * 1000
        total_dur += dur

    combined_path = os.path.join(OUT, "subtitles", "combined.srt")
    with open(combined_path, "w") as f:
        f.write("\n\n".join(entries) + "\n")
    print(f"[3/5] 字幕已合并 (总时长 {total_dur:.1f}s)")


def create_segments(slides):
    """ffmpeg image+audio → mp4 per slide with -t for exact duration."""
    seg_dir = os.path.join(OUT, "segments")
    count = 0
    for num in sorted(slides.keys()):
        if slides[num]["skip"]:
            continue
        img = os.path.join(OUT, "slides", f"index_{num:02d}.png")
        audio = os.path.join(OUT, "audio", f"slide_{num:02d}.mp3")
        seg = os.path.join(seg_dir, f"segment_{num:02d}.mp4")
        dur = get_duration(audio)
        run(
            f"ffmpeg -y -loop 1 -i {img} -i {audio} "
            f"-c:v libx264 -tune stillimage -pix_fmt yuv420p "
            f"-c:a aac -b:a 192k -t {dur:.6f} -movflags +faststart {seg}",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        count += 1
    print(f"[4/5] 视频片段已生成 ({count} 个 mp4)")


def concat_and_subtitle(slides):
    """Concat segments + burn subtitles."""
    seg_dir = os.path.join(OUT, "segments")
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for num in sorted(slides.keys()):
            if slides[num]["skip"]:
                continue
            f.write(f"file 'segment_{num:02d}.mp4'\n")

    no_subs = os.path.join(OUT, "video-nosubs.mp4")
    run(
        f"ffmpeg -y -f concat -safe 0 -i {concat_file} -c copy {no_subs}",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    srt_path = os.path.join(OUT, "subtitles", "combined.srt")
    ass_path = os.path.join(OUT, "subtitles", "combined.ass")
    run(f'ffmpeg -y -i {srt_path} {ass_path}',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with open(ass_path) as f:
        ass_content = f.read()
    old_style = re.search(r'Style: Default,[^\n]+', ass_content)
    if old_style:
        ass_content = ass_content.replace(old_style.group(), ASS_STYLE)
    with open(ass_path, "w") as f:
        f.write(ass_content)

    final = os.path.join(OUT, "final-video.mp4")
    ass_abs = os.path.abspath(ass_path).replace(":", "\\:")
    run(
        f'ffmpeg -y -i {no_subs} -vf "subtitles=\'{ass_abs}\'" '
        f'-c:v libx264 -crf 18 -preset medium -c:a copy {final}',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    dur = get_duration(final)
    print(f"[5/5] 最终视频已生成: {final}")
    print(f"      总时长: {dur:.1f}s ({dur/60:.1f} min)")


def run_build(step=None):
    """Parse review.md and run the build pipeline."""
    config, slides = parse_review()

    active = sum(1 for s in slides.values() if not s["skip"])
    skipped = sum(1 for s in slides.values() if s["skip"])
    print(f"解析 review.md: {active} 页有效, {skipped} 页跳过")
    print(f"  语音: {config.get('voice')}, 语速: {config.get('rate')}")
    print()

    steps = [
        lambda: write_narrations(slides),
        lambda: generate_audio(config, slides),
        lambda: build_combined_srt(slides),
        lambda: create_segments(slides),
        lambda: concat_and_subtitle(slides),
    ]

    if step is not None:
        if 1 <= step <= len(steps):
            print(f"=== 执行 build 步骤 {step} ===\n")
            steps[step - 1]()
        else:
            print(f"步骤编号无效，范围 1-{len(steps)}")
            sys.exit(1)
    else:
        print("=== 开始构建视频 ===\n")
        for s in steps:
            s()
        print("\n完成！")


# ─── Main ──────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python build_video.py review            # 生成确认文档")
        print("  python build_video.py build             # 解析确认文档并生成视频")
        print("  python build_video.py build --step 3    # 仅执行步骤 3")
        print("  python build_video.py check             # 仅检查依赖")
        sys.exit(0)

    cmd = args[0]

    if cmd == "check":
        check_deps()

    elif cmd == "review":
        check_deps()
        ensure_dirs()
        generate_review()

    elif cmd == "build":
        check_deps()
        ensure_dirs()
        step = None
        if "--step" in args:
            idx = args.index("--step")
            if idx + 1 < len(args):
                step = int(args[idx + 1])
        run_build(step)

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: review, build, check")
        sys.exit(1)


if __name__ == "__main__":
    main()
