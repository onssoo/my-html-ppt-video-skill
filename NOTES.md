# 本地化说明

来源：https://github.com/juguang/html-ppt-video-skill （master 分支）
上游文件：SKILL.md（212 行）+ build_video.py（476 行）

本目录：
  SKILL.md            改写版 —— 依赖表/流程/配置/坑 全部按 M2 实测结果重写
  ppt2video.py        替代上游 build_video.py（保留审稿流程，见 SKILL.md 文末「与上游的差异」）
  narration-spec.md   解说词规范 —— 正式陈述的语气/语言/结构/事实/免责/书写规则 + lint 清单
  NOTES.md            本文件

上游 build_video.py 未采用的原因：
  1. edge-tts 是在线微软 TTS，无法克隆本人音色
  2. 依赖 Google Chrome（M2 只有 Edge）与 render.sh
  3. 依赖 brew 的 ffmpeg/ffprobe（M2 无 brew）
  4. 逐页 mp4 片段 + concat，上游自己的文档就写了 -shortest 会漂移
  5. 字幕按页估算时长，精度不足

本地版实测记录见 SKILL.md「Critical Pitfalls」（每条都有具体数字）。

解说词规范（narration-spec.md）要点：
  - 正式口头陈述，不是聊天：严谨/正向/自信/适度激情
  - 只用陈述句（无问句/自问自答）、无套话、无俚语、不提及画面
  - 区分事实与预测，数字口径一致（brief.md 口径表），竞争对手中性描述
  - 免责语只在 data-disclaimer 页
  - 书面数字由 speak()（cn2an + 单位规则 + pronounce.json）转读法；字幕留书面
  - lint 自动检查，不合格重写（最多 2 次）
