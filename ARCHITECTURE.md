# entertain_cut 架构文档

## 目标

把横屏/竖屏娱乐素材加工成标准竖屏娱乐营销号短视频：

- 输出 9:16 竖屏，当前规格为 `1080x1920`、`30fps`
- 用横屏原片做模糊背景垫底
- 中间保留人物主体，裁掉边缘水印、原字幕条、广告角标等杂乱元素
- 增加双语字幕，中文主字幕、英文副字幕，并高亮关键词
- 增加娱乐号包装元素，包括标题、贴纸、亮色边框、底部品牌
- 底部品牌名固定为 `KC娱乐`
- 底部 CTA 固定为 `喜欢记得点关注`
- 支持单条原片包装，也支持多明星混剪；混剪可插入 `No.1/No.2/...` 转场卡和顶部人物导航条

## 目录结构

```text
entertain_cut/
  原视频.mp4                         # 输入原片
  KC娱乐_王濛一句话把全场整笑了.mp4    # 当前最终输出
  KC娱乐_男明星说英语你Pick谁.mp4      # 多明星混剪输出，不提交 git
  KC娱乐_女明星说英语你Pick谁.mp4      # 多明星混剪输出，不提交 git
  KC娱乐_日语韩语英语法语方言全能语言小天才王一博.mp4  # 多语言混剪输出，不提交 git
  KC娱乐_基因遗传的神奇像不像父亲母亲与女儿DontBreakMyHeart.mp4  # 亲子同曲对照输出，不提交 git
  generate_caption_plan.py           # 生成/校正字幕计划
  render_entertain_vertical.py        # 竖屏包装与视频合成
  render_star_english_mix.py          # 多明星英语名场面混剪模板/男明星入口
  render_female_star_english_mix.py   # 女明星英语名场面混剪入口
  render_wangyibo_language_mix.py     # 王一博多语言混剪入口
  render_dou_family_heart_mix.py      # 窦唯/王菲/窦靖童同曲亲子对照入口
  ARCHITECTURE.md                    # 本文档
  待混剪/                            # 多明星混剪输入素材，不提交 git
    肖战.mp4
    王一博.mp4
    龚俊.mp4
    丁禹兮.mp4
  work/
    asr/
      audio_16k_mono.wav             # 从原片提取的 16k 单声道音频
      transcript.json                # whisper-cli 原始转写
      transcript.srt
      transcript.txt
    caption_plan.json                # 最终字幕计划
    render/
      static_overlay.png             # 标题、贴纸、品牌等静态包装层
      subtitle_*.png                 # 每段字幕透明层
    frames/
      frame_*.jpg                    # 原片抽帧
      final*_*.jpg                   # 成片抽帧检查
    mix/                             # 多明星混剪中间产物，不提交 git
      asr/
      clips/
      frames/
      render/
```

## 处理流水线

1. **音频提取**

   使用 `ffmpeg` 从 `原视频.mp4` 提取 16k 单声道音频：

   ```bash
   ffmpeg -i 原视频.mp4 -vn -ac 1 -ar 16000 work/asr/audio_16k_mono.wav
   ```

2. **音频转写**

   使用本机 `whisper-cli` 和 `/Users/ytfeng/Models/whisper/ggml-small.bin` 转写音频，按素材语言选择 `-l en` 或 `-l zh`，产物保存在 `work/asr/transcript.*` 或当前任务命名的 `current_transcript.*`。

3. **字幕计划生成**

   `generate_caption_plan.py` 读取 Whisper 转写，优先用 DeepSeek 修正 ASR、生成中文娱乐号字幕、高亮词和标题。

   DeepSeek key 只通过环境变量传入，不写入项目文件：

   ```bash
   DEEPSEEK_API_KEY='...' python3 generate_caption_plan.py
   ```

   如果 DeepSeek 不可用，脚本会用内置 fallback 字幕计划，保证后续渲染不中断。

4. **透明包装层生成**

   `render_entertain_vertical.py` 使用 Pillow 生成：

   - `static_overlay.png`：顶部标题、HOT 贴纸、笑点标签、底部 `KC娱乐`
   - `subtitle_*.png`：每段双语字幕透明图层

5. **视频合成**

   `render_entertain_vertical.py` 调用 `ffmpeg` 合成最终视频：

   - 背景层：原横屏铺满竖屏、强模糊、加深、提高饱和
   - 主体层：从原片中间偏右裁剪人物区域，再缩放叠加
   - 包装层：叠加静态 PNG
   - 字幕层：按 `caption_plan.json` 的时间轴动态叠加
   - 音频层：做简单人声增强、压缩和音量提升
   - 时长：运行时通过 `ffprobe` 自动读取 `原视频.mp4`
   - 截断：如源片结尾有黑场，可传 `--duration <秒数>` 截到有效内容结束
   - 版本化增强：极轻微亮度、对比度、饱和度、锐化、噪声、动态缩放和音频 EQ/音量微调

## 渲染层结构

从底到顶：

```text
模糊横屏背景
  -> 中间人物主体裁剪层
  -> 静态娱乐包装层
  -> 当前时间段字幕层
```

主体裁剪在 `render_entertain_vertical.py` 的 `build_filter_complex()` 中配置：

横屏源默认主体裁剪：

```text
crop=760:560:320:20
```

竖屏源默认主体布局：

```text
scale=1080:1440:force_original_aspect_ratio=increase,crop=1080:1440
```

含义：

- 从原片中裁出宽 `760`、高 `560`
- 左上角横坐标为 `x=320`
- 左上角纵坐标为 `y=20`
- 纵向少取底部区域，用来减少原节目字幕条、广告角标和贴图残留
- 横向保持中间略偏右，兼顾主讲人和反应镜头
- 对竖屏访谈类素材，主体层保留中部 1080x1440 区域，上方给标题留出刘海屏安全区，下方给字幕和品牌区留空间

## 包装层位置

- 顶部标题整体下移到刘海屏安全区以下：主标题约 `y=188`，副标题约 `y=276`
- 栏目条 `lower_ribbon` 位于底部信息区上方，约 `y=1579`
- CTA `喜欢记得点关注` 位于栏目条和品牌区之间，约 `y=1674`，字号大于栏目条
- 品牌 `KC娱乐` 位于最底部品牌区，约 `y=1806`

## 多明星混剪

`render_star_english_mix.py` 用于「明星说英语，你Pick谁？」这类多素材混剪。它不读取 `work/caption_plan.json`，而是在脚本顶部的 `SEGMENTS` 里直接声明素材、转场卡、截取时间、裁切参数和字幕。

`render_female_star_english_mix.py` 复用同一套 KC 娱乐模板，运行前覆盖 `WORK_DIR`、`OUTPUT`、`NAV_ITEMS`、`TITLE_MAIN`、`TITLE_SUB`、`LOWER_RIBBON`、`TITLE_HIGHLIGHTS` 和 `SEGMENTS`，用于生成「女明星说英语，你Pick谁？」。

`render_wangyibo_language_mix.py` 同样复用该模板，用于生成「日语、韩语、英语、法语、方言，全能语言小天才王一博」。它会把顶部常驻标题缩短为两行，导航项改为 `日语/韩语/英语/法语/方言`，并把贴纸文案从 `英语` 覆盖为 `语言`。

`render_dou_family_heart_mix.py` 复用同一套模板，用于生成「不得不感叹基因遗传的神奇～像不像？父亲母亲与女儿《Don't Break My Heart》」。它按 `窦唯/王菲/窦靖童` 做父亲、母亲、女儿同曲对照，顶部标题缩短为 `基因遗传太神奇 / 像不像？`，字幕使用 KC 娱乐评论式文案，不直接照搬整段歌词。

当前混剪约定：

- 输入素材放在 `待混剪/`，按人物命名，例如 `肖战.mp4`、`王一博.mp4`
- 输出由入口脚本控制，例如 `KC娱乐_男明星说英语你Pick谁.mp4` 或 `KC娱乐_女明星说英语你Pick谁.mp4`
- 输入视频、输出视频和 `work/` 中间产物均由 `.gitignore` 排除，不提交 GitHub
- `kind="card"` 表示转场卡，`kind="clip"` 表示视频段
- 同一个人物可以拆成多个连续 `clip`，用于避开噪声、脏画面或无关片段
- `source/start/duration/crop` 决定取哪段素材和怎么裁切
- `crop.fit = "contain"` 可用于竖屏近脸素材，保留原片完整宽高比例，再居中放入主画面区域
- `subtitles` 手动维护中英双语字幕和高亮词，优先人工校正，不盲信 ASR

混剪包装层规则：

- 顶部标题由 `TITLE_MAIN / TITLE_SUB` 控制，例如 `男明星说英语 / 你Pick谁？` 或 `女明星说英语 / 你Pick谁？`
- 顶部导航条由 `NAV_ITEMS` 控制，当前人物高亮，已出现人物显示青色进度线
- 每段视频右上方有人物标签，例如 `No.3 龚俊`
- 主视频上缘有深色遮罩，用于压掉原片营销标题、平台字和无关贴纸
- 字幕区与底部品牌区使用不透明深色底板，防止原片字幕、水印透出；底部遮罩从 `y=1206` 开始压住多数原片字幕条
- 字幕位置由 `CAPTION_Y` 控制，中文主字幕大字，英文副字幕小字，关键词黄色高亮

混剪渲染命令：

```bash
python3 render_star_english_mix.py
ffmpeg -i KC娱乐_男明星说英语你Pick谁.mp4 -r 30 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -ar 48000 -movflags +faststart KC娱乐_男明星说英语你Pick谁_30fps.mp4

python3 render_female_star_english_mix.py
ffmpeg -i KC娱乐_女明星说英语你Pick谁.mp4 -r 30 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -ar 48000 -movflags +faststart KC娱乐_女明星说英语你Pick谁_30fps.mp4

python3 render_wangyibo_language_mix.py
ffmpeg -i KC娱乐_日语韩语英语法语方言全能语言小天才王一博.mp4 -r 30 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -ar 48000 -movflags +faststart KC娱乐_日语韩语英语法语方言全能语言小天才王一博_30fps.mp4

python3 render_dou_family_heart_mix.py
ffmpeg -i KC娱乐_基因遗传的神奇像不像父亲母亲与女儿DontBreakMyHeart.mp4 -r 30 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -ar 48000 -movflags +faststart KC娱乐_基因遗传的神奇像不像父亲母亲与女儿DontBreakMyHeart_30fps.mp4
```

混剪质检：

1. 用 `ffmpeg -ss <秒> -frames:v 1` 抽四位人物段落和片尾帧
2. 检查顶部导航当前人物是否高亮
3. 检查原片顶部营销字、底部水印、原字幕是否被遮住
4. 检查中英文字幕是否和原声对应
5. 用 `ffprobe` 确认最终输出为 `1080x1920`、`30fps`

## 版本化增强 / de_dupe

参考 `docs/short_video_dedupe_checklist.xlsx` 中“授权素材的版本管理、创作增强、批处理质检与合规发布”边界，当前脚本只采用轻微、可感知风险低的处理：

- 画面：`brightness=0.012`、`contrast=1.035`、`saturation=1.025`、`gamma=1.006`
- 背景：模糊背景轻微亮度/对比/饱和调整，并叠加低强度噪声 `noise=alls=1.4`
- 动态：主体层使用极轻微呼吸缩放 `1.004 + 0.003*sin(on/90)`
- 清晰度：主体层低强度 `unsharp=5:5:0.18`
- 音频：轻微 highpass/lowpass、轻压缩、微 EQ、`volume=1.018`

注意：该处理用于自有/授权素材的观感统一和版本管理，不用于侵权搬运、掩盖来源或规避平台审核。

## 字幕计划格式

`work/caption_plan.json` 是渲染时唯一读取的字幕数据源：

```json
{
  "title_lines": ["王濛一句话", "把全场整笑了"],
  "title_highlights": ["王濛", "整笑"],
  "top_badge": "反应名场面",
  "lower_ribbon": "综艺爆梗 · 王濛现场",
  "subtitles": [
    {
      "index": 1,
      "start": 0.0,
      "end": 2.2,
      "en": "The vibe starts the moment Wang Meng speaks.",
      "zh": "王濛一开口，气氛来了",
      "en_highlights": ["Wang Meng", "vibe"],
      "zh_highlights": ["王濛", "气氛"]
    }
  ]
}
```

修改字幕、标题、高亮词时，优先改 `caption_plan.json`，然后重新运行：

```bash
python3 render_entertain_vertical.py
```

## 关键依赖

- `ffmpeg` / `ffprobe`
- `whisper-cli`
- Whisper 模型：`/Users/ytfeng/Models/whisper/ggml-small.bin`
- Python 3
- Python 包：`Pillow`
- 字体：
  - 中文：`/System/Library/Fonts/Hiragino Sans GB.ttc`
  - 英文：`/System/Library/Fonts/Supplemental/Arial Bold.ttf`

## 复用方式

同类短视频复用时：

1. 替换 `原视频.mp4`
2. 重新提取音频并跑 Whisper
3. 运行 `generate_caption_plan.py`
4. 根据抽帧微调 `render_entertain_vertical.py` 里的主体裁剪参数
5. 运行 `render_entertain_vertical.py`，如结尾有黑场则追加 `--duration`
6. 抽取首中尾关键帧检查字幕、人物、安全区和品牌

## 安全与密钥

- DeepSeek API key 不写入脚本和 JSON
- 只通过 `DEEPSEEK_API_KEY` 环境变量使用
- `work/caption_plan.json` 只保存字幕内容，不保存密钥

## 当前输出

当前多明星成片：

```text
KC娱乐_男明星说英语你Pick谁.mp4
KC娱乐_女明星说英语你Pick谁.mp4
KC娱乐_日语韩语英语法语方言全能语言小天才王一博.mp4
```

王一博多语言混剪已检查规格：

- 分辨率：`1080x1920`
- 帧率：`30fps`
- 时长：约 `56.73s`
- 音频：已保留并增强
