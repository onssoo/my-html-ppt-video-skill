# 本地化说明

来源：https://github.com/juguang/html-ppt-video-skill （master 分支）
上游文件：SKILL.md（212 行）+ build_video.py（476 行）

本目录：
  SKILL.md        改写版 —— 依赖表/流程/配置/坑 全部按 M2 实测结果重写
  ppt2video.py    替代上游 build_video.py（保留审稿流程，改动 12 处，见 SKILL.md 文末）
  NOTES.md        本文件

上游 build_video.py 未采用的原因：
  1. edge-tts 是在线微软 TTS，无法克隆本人音色
  2. 依赖 Google Chrome（M2 只有 Edge）与 render.sh
  3. 依赖 brew 的 ffmpeg/ffprobe（M2 无 brew）
  4. 逐页 mp4 片段 + concat，上游自己的文档就写了 -shortest 会漂移
  5. 字幕按页估算时长，精度不足

本地版实测记录见 SKILL.md「Critical Pitfalls」（每条都有具体数字）。
