# 秒播基准方案（严格对齐 v0.2.42）

> **项目不可突破的永久底线：无论新增功能、性能优化、缺陷修复还是重构，都必须坚守 GitHub v0.2.42 的秒播策略。**
> 任何方案与该底线冲突时，先停下并征求用户，不能自行牺牲播放体验。
> 项目级强制规则同时固化在根目录 `AGENTS.md`；交付必须运行 `tests/test_playback_baseline_242.py` 与全量回归。

用户实测 v0.2.42 是秒播最快、海报墙瞬间加载的版本。全量后台预热与服务端磁盘
缓存都是负优化（抢占事件循环/连接池、引入协商往返与同步 IO），已全部移除。

## 一、海报机制（与 v0.2.42 完全一致）

- 纯同源透传：请求即回源图床，服务端【不做任何内存/磁盘缓存】；
- 仅响应 `Cache-Control: max-age=86400`，无 ETag、无 304 协商，浏览器有效期内零请求；
- 保留独立海报 HTTP 客户端（连接池与播放隔离），但不缓存内容；
- 豆瓣等图床按需带 Referer；Pillow 角标仅网盘少数图片触发。

## 二、播放清单缓存（仅内存，对齐 v0.2.42）

- `_PLAYLIST_CACHE` 内存缓存，TTL 120s，上限 600 条；冠军键享 15min stale 宽限；
- 无任何磁盘落盘，重启后靠现场竞速重建。

## 三、固化的竞速参数（DEFAULTS）

| 参数 | 值 |
|---|---|
| `line_fastest_timeout` | 4.0s |
| `line_fastest_max` | 6 |
| `line_fastest_prefer` | 0.25 |
| `_RACE_CONCURRENCY` | 4 |
| 海报/搜索后台预热 | 已禁用（v0.2.42 按需模式）|

## 四、已移除的负优化（禁止恢复）

- 全量后台预热：`prewarm_hot_full` / `prewarm_catalog_full` 及任务；
- 海报磁盘缓存 `poster_cache.py`、冠军清单磁盘缓存 `champion_cache.py`；
- 海报 ETag/304/immutable 协商；
- 相关配置键已加入 config `_DEPRECATED`，老配置升级自动清除。

## 五、保留的按需能力（无后台常驻重任务）

- linepredict 仅记录真实播放竞速结果；海报/搜索列表不再触发 `schedule_prewarm_title`；
- `start_fill_lines` 仅做同名同年线路数据聚合（最多2并发），不得改变竞速/选线/清单/分片策略；`_prewarm_from_info` / `_warm_preset_lines` 保持 no-op；
- T4 源、fnusers 子账号、chartbind 绑定：按需调用。
- TVBox 式二级线路目录只改变展示与入口：一级使用独立 `fnmedia-line-folder:` 目录 guid，不改变原影片 guid 的详情/播放/历史身份；二级按资源站聚合同源解析线/m3u8线，每来源一张卡并优先 m3u8/直放线；电影线路卡为 Movie/VOD，电视剧、动漫、综艺等多集内容统一保持 TV、真实季/集数，点击即播当前续播集；已绑定影片毫秒级返回，冷片首次进入在本次请求内等待绑定，禁止先返回空目录造成“无资源”误解；Season/Episode 使用带线路身份的 guid，每集绑定同一来源线路；固定线路 guid 保留原影片/集身份，play/play 与 media/range 不带 auto=1，禁止竞速和历史最优覆盖，播放记录不得按线路拆分。
- 播放进度/收藏/已看按基础影片/集身份记录：复合 guid `fnmedia-line-item:`/`fnmedia-line-season:` 经 `_normalize_history_guid` 归一化（多集→对应 `episode_guid`，电影→影片 guid）；复播用 `resume_episode` 精准定位到上次未看完的集与 ts，动漫/综艺/剧集共用同一多集续播语义。
- PanSou 网盘高清是虚拟手动线（不在 store、不进全局搜索、不参与测速、永不作默认）。完整版在二级线路墙末尾追加一张网盘卡；公开 lite 构建只保留恒返回 None 的 `_vod_pansou_card` 桩并整文件剥离 `pansou.py`，绝不出现网盘卡。点击网盘卡走 user_pick → arm → 搜索 → 转存原画；`_fixed_media_target` 对 `builtin-pansou::` 返回虚拟 target，stream/list 菜单阶段不预热网盘线。

## 六、2026-11-07 对齐 GitHub v0.2.42 的主链路红线

1. App/iOS 的 `line + auto=1` 直接使用默认线，**不得强制竞速**；仅无显式 line 的网页自动起播才竞速。
2. 榜单绑定采用快源优先（8s 上限）、无可靠匹配再全源 25 秒兜底；完整同名优先，仅允许语言/清晰度后缀并校验年份，禁止任意片名包含（会把《突如其来》错绑为《突如其来的假期》）。快源优先只缩短绑定等待，不放宽匹配。
3. 普通 CMS `main.m3u8` 不得再套 8 秒占位，也不得用 `_MAIN_SERVE_FUT` 合并不同请求；真实清单失败按 242 返回 502/503。
4. 去广告只跑同步本地规则，禁止起播时读取分片 SPS 指纹。
5. 不得恢复海报预热、全量预热或任何服务端海报/清单磁盘缓存；线路菜单按242规则按资源站去重，不为展示条数改成多线串行预取。

## 七、回归验证

1. 216 项单测全过；2. 海报响应头仅 max-age、无 ETag；
3. 服务启动无逐片/无界后台任务，空闲 CPU 接近 0；
4. 实测 App 默认线起播从约 4.7s 降至 0.64s，二次起播毫秒级。
