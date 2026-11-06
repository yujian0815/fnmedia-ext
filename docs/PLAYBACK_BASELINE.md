# 秒播基准方案（严格对齐 v0.2.42）

> 用户实测 v0.2.42 是秒播最快、海报墙瞬间加载的版本。全量后台预热与服务端磁盘
> 缓存都是负优化（抢占事件循环/连接池、引入协商往返与同步 IO），已全部移除。

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
| 按需预热在途上限 | 24（信号量并发 3）|

## 四、已移除的负优化（禁止恢复）

- 全量后台预热：`prewarm_hot_full` / `prewarm_catalog_full` 及任务；
- 海报磁盘缓存 `poster_cache.py`、冠军清单磁盘缓存 `champion_cache.py`；
- 海报 ETag/304/immutable 协商；
- 相关配置键已加入 config `_DEPRECATED`，老配置升级自动清除。

## 五、保留的按需能力（无后台常驻重任务）

- `schedule_prewarm_title`：浏览/搜索可见时才轻量探活；
- linepredict 本片最快线记忆（可选 persist 落盘）；
- T4 源、fnusers 子账号、chartbind 绑定：按需调用。

## 六、回归红线

1. 160 单测全过；2. 海报响应头仅 max-age、无 ETag；
3. 服务启动无逐片/无界后台任务，空闲 CPU 接近 0；
4. 禁止重新引入全量预热或任何服务端海报/清单磁盘缓存作为默认行为。
