# xhs2vid — 低粉爆款「网友热议」视频流水线

参考 `最终成品示例.mp4` 的热评展开方式，把当天优先、最近 26 小时补足的低粉爆款笔记加工成竖屏热议视频；
整屏使用仓库统一的 KC娱乐品牌 format（1080x1920 / 30fps / h264+aac）。

## 每日 5 条 GitHub Action

工作流 `.github/workflows/xhs-lowfan-kc-daily.yml` 每天北京时间 20:37 首次运行，并在次日
00:37、04:37 对同一业务日自动补偿：

1. 8 个关键词 × 2 种排序 × 3 页，只搜索一次；对最多 20 位作者核验粉丝数。
2. 严格选择粉丝不超过 2 万、点赞不少于 200 的帖子；目标业务日帖子优先，不足时由最近 26 小时候选补齐。
3. 补偿运行会先下载、校验并合并同一业务日的部分 Artifact，只生成仍缺的条目；TikHub 用量按来源 run 累计，硬上限始终为 `99`。
4. 每条成片最多使用 3 组一级评论与真实内嵌回复；封面及每段评论使用不同角色声线。
5. 每条视频只向 APIMart 创建 1 张 `gpt-image-2 / 1K` 低价头像图集，本地派生所有评论头像；整批付费创建槽位不超过目标成片数（定时任务最多 5 次），候补超出后改用 Pillow 头像。
6. 成片完整解码并核验 1080×1920、H.264、AAC 后，上传并按远端大小复核到 `/我的坚果云/KC Desk Notes/Ops/YYYY-MM-DD/Portal 娱乐/`。常规娱乐 5 条和低粉爆款 5 条共用这一个日期目录；两条工作流都可通过仓库变量 `JIANGUOYUN_REMOTE_ROOT` 统一修改 `Ops` 根目录。常规工作流只清理 `KC娱乐_` 前缀的被替换成片，保留低粉视频。
7. 只有目标条数全部生成并上传成功，才更新跨日去重清单。

GitHub 定时任务即使排队跨过北京时间午夜，也按原计划时点确定业务日，不会把相邻两天写进同一目录。
手动补交历史日期时可填写 `output_date`；`resume_run_id` 支持逗号分隔的多个失败 run ID，并可用
`resume_artifact_date` 指明旧 Artifact 内部日期。恢复产物仍须逐条通过低粉/点赞证据、1080×1920、
H.264/AAC 和完整解码检查，且最终严格满额才上传。

手动运行时默认只做 1 条样片；定时运行固定做 5 条。需要配置仓库 Secrets：

- `TIKHUB_API_KEY`
- `APIMART_API_KEY`
- `JIANGUOYUN_WEBDAV_USER`（也兼容 `JIANGUOYUN_WEBDAV_USERNAME`）
- `JIANGUOYUN_WEBDAV_PASSWORD`
- 可选 `JIANGUOYUN_WEBDAV_URL`（默认 `https://dav.jianguoyun.com/dav/`）

本地端到端命令：

```bash
python3 xhs2vid/run_daily_batch.py \
  --limit 1 \
  --request-limit 90 \
  --avatar-provider apimart \
  --output-dir outputs/xhs_lowfan/$(TZ=Asia/Shanghai date +%F)
```

## 三步流水线

```bash
# 1. 选题: 多关键词搜"一天内"普通笔记, 聚合去重后按点赞排序,
#    对头部候选查作者粉丝数, 选 粉丝少+点赞高 的低粉爆款
#    产出 work/chosen_note.json / work/selected_notes.json / work/candidates.json
python3 xhs2vid/discover_note.py

# 2. 抓素材: 下载笔记封面, get_note_comments(sort_strategy=like_count)
#    取最热前三条评论 → work/cover.png / work/top_comments.json
python3 xhs2vid/fetch_assets.py

# 3. 渲染: 使用整屏 KC娱乐模板；叙事和素材展开方式参考示例成品，
#    段落 = [封面截图, 热评1, 热评2, 热评3]
#    - 深色 KC 标题区写笔记标题，按语义断成两行并自动高亮核心词
#    - 评论卡: 随机昵称和合成头像, 正文自适应字号, 时间·IP·回复·点赞·展开回复
#    - 正文随口播进度从模糊逐段展开为清晰，帮助读者跟读
#    - 字幕逐页(jieba 词边界切分 ≤16 字), 自动高亮关键词
#    - 每页 macOS say(Tingting) TTS，语速 278（原 185 的约 1.5 倍）
#    - 封面读完后中心火圈向外漫开，配示例中的爆炸声并揭示热评1
#    - 每条热评读完后快速中心放大，配示例中的相机快门声，再切下一条
#    产出 KC娱乐_<标题>.mp4
python3 xhs2vid/render_video.py
```

### TikHub 低于 100 次的全新选题

新建独立工作目录，并让发现、评论两个步骤共享目录内的 `tikhub_request_budget.json`。计数发生在每次
真实 HTTP 尝试之前，失败重试也会计入；硬上限必须小于 100。

```bash
RUN_DIR="xhs2vid/work/runs/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

# 16 次搜索 + 最多 10 次作者查询；每个调用最多尝试 2 次。
/usr/bin/python3 xhs2vid/discover_note.py "$RUN_DIR" \
  --pages 1 \
  --top-author-check 10 \
  --max-attempts 2 \
  --request-limit 90 \
  --strict-low-fan

# 1 次主评论请求；仅缺少内嵌子评论时才补抓，最多补 3 组。
/usr/bin/python3 xhs2vid/fetch_assets.py "$RUN_DIR" \
  --max-attempts 2 \
  --request-limit 90 \
  --max-subcomment-calls 3

# 新素材 + 真实子评论 + 样本 1 机械声。
/usr/bin/python3 xhs2vid/render_video.py \
  --work-dir "$RUN_DIR" \
  --voice jianying-machine \
  --speaker BV001_fast_streaming \
  --include-subcomments \
  --render-dir "$RUN_DIR/render_machine" \
  --output xhs2vid/outputs/低粉爆款_机械声.mp4
```

这组参数的最坏上界是 60 次 TikHub HTTP 尝试；若主评论响应已携带子评论预览，正常用量是
`16 次搜索 + 10 次作者 + 1 次评论 = 27 次`。封面从小红书 CDN 下载，不计 TikHub 付费请求。

### 机械声＋真实子评论样片

`comments_raw.json` 中缓存的子评论可以作为独立卡片接在对应热评后面。下面的命令使用剪映“小姐姐”
`BV001_fast_streaming`，并应用样本 1 的快、高音参数（+6.56 半音、净语速约 1.486 倍）。单独指定
中间目录和成片路径，不会覆盖默认渲染结果：

```bash
/usr/bin/python3 xhs2vid/render_video.py \
  --work-dir xhs2vid/work \
  --voice jianying-machine \
  --speaker BV001_fast_streaming \
  --include-subcomments \
  --render-dir xhs2vid/work/render_machine_sample \
  --output xhs2vid/样片_小红书热评与子评论_新机械声.mp4
```

该模式调用 `tools/machine_voice_tts.py` 生成 48 kHz Opus 原声，再用 FFmpeg 做轻量升调、加速和限幅；
不在本机加载语音模型。

## 依赖

- TikHub API key: `api_key/tikhub.txt`(用 `/api/v1/xiaohongshu/app_v2/*` 接口)
- python: `pip install -r requirements-xhs2vid.txt`
- 系统: ffmpeg/ffprobe；本机可用 macOS `say`，Action 使用剪映轻量在线 TTS
- 字体: macOS Hiragino Sans GB；Ubuntu Action 安装 Noto Sans CJK

## 模板几何

- 画布 1080x1920；顶部 KC 标题区 y∈[0,430)
- 中间白色阅读面板 y∈[430,1360)
- 字幕中心线 y=1292(白字黑描边, 压面板底部)
- y=1360 以下为 KC娱乐底栏：栏目条、`喜欢记得点关注` CTA、`KC娱乐 / ENTERTAINMENT` 品牌区
- `最终成品示例.mp4` 用于对齐“封面两句 → 火焰爆开 → 热评1/2/3 → 每条末尾放大+快门”的节奏
- 火焰爆炸声和相机快门声由脚本从示例的对应时间点截取，随画面关键帧同步混音
