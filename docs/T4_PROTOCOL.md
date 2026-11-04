# T4 协议层（系列 1/5）

基于公开上游 `main` 的 `28d35835c9766c2ad0a7c2812b912951e7430156`。
本提交只增加可显式调用的协议模块及模型字段，不注册来源、路由或媒体库，
不在应用启动时创建 T4 对象，不修改 CMS 搜索、播放或 `/seg` 行为。

## 调用边界

- `t4.request/home/category/search/detail/player` 使用调用方的异步 HTTP 客户端。
- 保留 API 地址已有查询参数；支持 GET、POST 和字符串/结构化 `ext`。
- 搜索只返回卡片，详情才展开分集，播放才向远程服务取链。
- 分类 `extend` 与插件初始化 `ext` 独立。
- `Episode.t4/episode_key`、`OnlineTitle.locator` 为默认值兼容的附加字段；
  `inject.media_type_name` 仅用于 T4 详情保留明确的电影/剧集类型。
- 稳定影片 ID、明确季集身份用于防止错片、错集；重复版本不按数组下标合并。
- 带头、私网媒体只标记交付要求，本阶段不代理媒体。
- 未完成的 `parse/jx`、多地址/双轨播放结果明确报错，不在扩展内运行爬虫或解码。
- 错误消息不带上游完整 URL、签名或凭证。

## 拆分顺序

1. 协议层和模型（本提交）。
2. 媒体交付及有界元数据探测。
3. 浏览、媒体库和按需详情/播放状态。
4. 宿主接入、订阅导入和 UI。
5. 共存回归、CI、选路说明与真机自测清单。

每阶段包含自身测试。后续 PR 在前序合入后重新基于上游最新 `main` 提交，
避免向上游同时打开多个包含前序全部差异的累计 PR。

## 本阶段验证

```console
python -m unittest discover -s tests -p test_t4_protocol.py -v
python -m compileall -q proxy tests
git diff --check
```

测试只使用 Mock HTTP，无真实订阅、生产配置或真机播放承诺。
