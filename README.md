# 飞牛影视 · 在线资源扩展 (fnmedia-ext)

给飞牛影视增加在线能力：媒体库首页注入 **4 个豆瓣榜单库 + 4 个资源站海量分类库**（无限滚动、点开即播），并在**原生搜索**里聚合苹果 CMS 资源站的在线影视，用飞牛自带播放器**直接播放**。搜索结果默认**在线前排**。零侵入、完全可逆：不改官方二进制 / nginx / 数据库，靠 Unix Socket 原子接管。

> 点播源会随时间失效，因此本扩展**不做转存/STRM 落库**——即刷即播、即搜即播；源不可用时可在管理页删除或替换。
>
> 📌 各版本更新内容见 [CHANGELOG.md](CHANGELOG.md)。

## 远程 T4 接入

T4 爬虫运行在外部服务端，扩展只接收标准 JSON 结果并对接飞牛原生目录与播放器；
不在 NAS 扩展内运行 Android/JAR/Python/JS 爬虫，不捆绑私有插件或账号。

1. 在管理页 `/app/fnmedia-ext/` 或 `/v/online-admin` 的「订阅导入」中粘贴包含
   `sites` 数组的统一订阅，例如 `http://t4.example:8765/t4/config.json`。
2. 所有 `type=4` HTTP(S) 来源均可登记，保留 `ext`、GET/POST 和搜索开关；
   同地址与 `ext` 去重，不设固定插件数量白名单。首页测试成功不等于影片可播。
3. 回到原生搜索，或打开自动出现的「T4 选片」，选择插件、分类、筛选和目录。
   首页/分类不支持的纯搜索来源仍走搜索入口；来源同名不会合并身份。
4. 选中影片才加载详情，明确季集映射到原生选集，同集切线不按文件下标猜测；
   按播放请求取链。收藏/登录等 action 卡片不伪装成影片。

T4 选片继承已有媒体库隐藏、排序和子账号权限；原生搜索沿用在线搜索授权，
历史沿用上游分用户记录。新增详情仍使用已有防抖写盘和缓存上限，不在搜索热路径同步写盘。
普通 T4 不依赖 Sidecar：只有导航卡片的 `t4_action.type=search`、增量搜源和持久内容身份等
扩展协议需要相应服务端支持。配套 Sidecar 0.3.11 可首批返回、滚动追加结果，
已显示卡片不重排；插件范围在服务端选择，不在飞牛重复配置。

### 媒体交付与性能

- 公网无头直链交给客户端；带头或私网媒体按需流式代理，支持 Range/HEAD、
  嵌套 HLS、音轨、分片和密钥请求。头信息不写入最终播放地址或历史。
- 容器类型以最多 4 KiB 实际字节识别，不凭扩展名伪报 MP4/HLS。可选容器元数据
  最多读取 8 MiB、限时 8 秒、最多两路；只使用剩余起播预算，超时后台补齐并回收。
- 搜索最多 8 路，T4/CMS 交替调度，有 T4 时最多 5 秒收集已完成结果并取消剩余请求；
  浏览最多 4 路、列表缓存 60 秒且限制体积，详情/取链请求合并。
- 不解码、不转码、不重封装，不修改媒体字节；客户端仍须支持实际容器和编码。
  日志记录客户端、阶段与耗时，播放参数脱敏。
- HTTPS 反代须经扩展所在的 `/v` 入口，传递完整 `X-Forwarded-Host`（含端口）和
  `X-Forwarded-Proto`；不应直连绕过 Socket 扩展的官方后端。原文件返回完整地址，
  不要求原生客户端补全相对路径，不另设 Gateway Token。

### 适配与验证

支持 T4 首页、分类/筛选、搜索、详情、多季多线路、HLS/MP4/MKV 网络交付。
上游须完成客户端嗅探/解析任务；DASH/双轨、外挂字幕、直播和 AT action 写回不在本次承诺范围。
标准 T4 可独立使用，AT 特殊能力是否可用由其 T4 服务端实现决定。
既有 fork 的 NAS 验证不是本贡献分支的宿主部署验收；UA 回归与 HTTP 200/206 也不等于
TV/Android/iOS 真机解码或长播通过。当前 HEVC 浏览器样本不支持，需匹配客户端或更换线路。

运行协议与合成媒体回归：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q proxy tests
```

测试使用临时状态、Mock 接口和回环 HTTP，不读取生产凭证、不接管官方 Socket。
可选媒体夹具缺少 `ffmpeg`/`ffprobe` 时跳过，运行服务不要求它们进行视频转码。
GitHub Actions 同时提供 Windows/Ubuntu、Python 3.10/3.12 回归与 FPK 构建。

> ⚠️ **免责声明**：本项目是开源免费的技术研究 / 个人媒体库工具，**本身不提供、不存储、不分发任何影视、图片或播放地址，也不内置资源数据库**；所有在线内容均来自互联网公开的第三方接口与公开榜单，版权归原权利人所有。本项目**不提供、不推荐、不解答任何资源站点的名称、地址或获取方法**，接口由使用者自行配置并自行核实合法性。仅供学习交流与个人合法观看自有 / 已授权内容，请勿商用或传播侵权内容；由此产生的法律责任由使用者自行承担。

## 适配版本

| 项目 | 已验证 / 要求 |
|---|---|
| fnOS | **1.2.0 及以上**（FPK manifest 最低要求） |
| 官方影视应用 | **trim.media `0.9.8-1`**（已实测） |
| CPU 架构 | `all`（纯 Python，安装时创建独立 venv） |
| 安装方式 | fnOS 应用中心手动安装 FPK，可安装在 `/vol1`、`/vol2` 等数据卷 |
| 依赖 | 官方「影视 (trim.media)」必须保持安装并启用 |
| 网络 | 资源站访问使用直连（`trust_env=False`），不读取代理环境变量 |

> 本扩展通过 Unix Socket 可逆接管官方影视接口，不改官方二进制、nginx 或数据库。未来若 fnOS / trim.media 升级后签名常量或接口结构变化，停用扩展会立即恢复官方服务，等待新版本适配。

## 下载安装

1. 打开 [Releases](https://github.com/yujian0815/fnmedia-ext/releases)，下载最新的 `fnmedia-ext-<version>.fpk`。
2. 在 fnOS **应用中心 → 手动安装 FPK** 中选择该文件。
3. 选择应用安装所在存储卷，保持依赖应用 **影视 (trim.media)** 为运行状态。
4. 安装完成后启用「飞牛影视在线扩展」，桌面/应用内会出现 **影视在线扩展管理**。
5. 打开官方「影视」，直接搜索中文片名即可看到带 `【在线】` 标记的结果。

管理入口为应用内 iframe：`/app/fnmedia-ext/`；旧的直连路径 `/v/online-admin` 仍保留。卸载或停用扩展会自动把 `/var/run/trim_media.sock` 还原给官方影视服务。

## 工作原理

```
浏览器/飞牛App → nginx(/v) → /var/run/trim_media.sock → fnmedia-ext(FastAPI)
                                                   ├─ 普通请求 → 透传官方后端(upstream socket)
                                                   ├─ /search/list → 注入在线结果（默认排最前）+ 本地结果
                                                   ├─ /item /season/* /episode/* /play/* /stream/* → 伪造链路
                                                   ├─ mediadb/list · mediadb/sum · item/list · item/<guid>
                                                   │      → 注入豆瓣榜单四库 + 资源站海量分类四库（无限滚动）
                                                   └─ /v/media/online/* → HLS/分片/密钥/mp4 反向代理
```

### 热门榜单媒体库（展示 / 播放解耦，对齐 LibreTV）

“热门”不再用资源站的「最近更新」冒充（那是“刚录入了什么”，不是“大家在看什么”，且电影稀少、
海报差、无评分）。改为与 [LibreTV](https://github.com/LibreSpark/LibreTV) 相同的两层设计：

- **展示层（真实榜单）**：后台定时（默认每 6 小时）拉取豆瓣周榜 + 豆瓣官方 tag 推荐（经 60s 公开 API
  免 key 转发，实例失败自动回退），合成 4 个虚拟库 **🎬热门电影 / 📺热门剧集 / 🌸动漫新番 / 🎤热门综艺**，
  每库最多 160 部（豆瓣 tag 多标签多页聚合去重，综艺去重后约 130+），含豆瓣海报、评分、年份、简介；原子缓存到 `state/charts.json`，媒体库只读、秒开。
- **播放层（按需解析）**：榜单条目的 guid 是稳定的 chart guid，**点进详情时才用片名实时聚合搜索内置
  资源站**（复用搜索的跨源合并与相关性过滤），命中后把可播放 brief 挂到该 guid，复用既有
  详情/选集/多线路/续播/代理播放链；首次解析约几秒，之后命中内存/持久缓存秒开。源站没有的
  （部分新综艺）返回 can_play=0 与「暂无在线片源」，不误导用户。
- 容错：榜单全部失败/为空时保留旧缓存；豆瓣图床经 `/online/` 海报代理并带豆瓣 Referer，
  失败回退内联 SVG（HTTP 200）。兼容官方 `page/page_size` 分页、`tags.type`、`sort_column`
  （榜单次序即热度，也可按评分/年份/片名排序）。
- 管理页可开关、设置榜单刷新间隔(1-24h)、立即刷新。旧的“最近更新”库 guid 仍可打开（映射到热门剧集）。

### 海量分类片库（方案 C · 资源站全量深翻，无限滚动）

除精选榜单外，额外注入 4 个海量库 **🎞海量电影 / 📺海量剧集 / 🌸海量动漫 / 🎤海量综艺**，用于“一直往下刷”：

- 启动/每 6 小时拉取各资源站苹果 CMS 分类表 `ac=list`，把全部子分类按名称归并到四大类，
  自动剔除伦理/福利/解说/预告/体育/短剧等脏类；每类探测各源子类第一页，选页数最多的作“规范源”
  （实测电影/剧/动漫可达 250 页≈**5000 部**、综艺 147 页≈2940），其余源作回退。
- 浏览时按页直连 `ac=detail&t=<子类id>&pg=N`（每页 20、详情自带播放地址，**点开即可播、无需再搜索**）；
  页级 10 分钟内存缓存 + 单 flight，规范源某页失败自动换源。
- 适配官方前端**虚拟列表无限滚动**：前端按 22 条/页块请求，服务端用“全局偏移跨 CMS 页取窗 +
  会话级 guid 去重 + 不足一窗自动补页”，保证滚动连续、每部片全局只出现一次、到底自然停止。
- 条目 guid 绑定 `(源, vod_id)` 并入库，复用既有详情/选集/多线路/续播/代理播放链。

- `supervise.py` 先校验官方 socket 的监听者 exe，再在私有暂存 socket 启动本服务；
  健康检查通过后用 `renameat2(NOREPLACE)` 原子换名接管；退出/崩溃时原子换回官方 socket（fail-closed）。
- 播放：HLS 源返回自研 `play_link=/v/media/online/<media_guid>/preset.m3u8`，
  服务端实时抓取源站 m3u8，把分片/密钥 URI 重写为同源代理地址（透传 Referer/UA/Range）；
  mp4 等非 HLS 直链返回 `/direct`，流式反向代理并透传 `Range`（支持拖动进度，206 直通）。
- 在线条目 guid = `md5("on_<provider>_<vodid>")`，media/video/season/episode guid 确定性派生、可反查。
- 标题与线路持久化在 `state/titles.json`，重启后历史卡片仍可播放。

## 目录

```
proxy/
  app.py            主代理：透传 + 搜索注入 + 播放伪造 + HLS/海报代理 + 管理 API
  supervise.py      原子接管守护（暂存启动→校验→换名；退出自动回滚）
  providers.py      苹果CMS V10 适配 + 连通性探测 + /share 播放页解析（直连国内，trust_env=False）
  inject.py         在线条目 → 飞牛 item/stream/play/history 结构（标题带【在线】）
  store.py          标题/线路持久化、跨源聚合、按集解析、线路/集反查索引
  hot.py            「在线热门」后台刷新：跨源聚合去重、排序、原子缓存、失败保留旧缓存
  history.py        在线播放进度（继续观看）：record 落库、play/list 合并、ts 续播
  config.py         可热加载配置：自定义源增删、启用开关、噪音过滤、在线前排
  admin_ui.py       单文件管理页（源管理/测速/设置）
  fnmedia_authx.py  官方 authx 签名客户端（MD5 + apiKey）
deploy/             持久 systemd 单元 + install/uninstall
packaging/fpk/      飞牛应用中心 FPK 打包
state/              titles.json / config.json（运行时生成）
```

## 从源码构建 FPK

普通用户建议直接下载 Release 的 `.fpk`。开发者可从源码构建：

```bash
git clone https://github.com/yujian0815/fnmedia-ext.git
cd fnmedia-ext
bash packaging/fpk/build.sh
# 产物：dist/fnmedia-ext-<version>.fpk
```

FPK 安装回调会自动在应用目录创建独立 venv，并安装 `requirements.txt` 中声明的运行依赖。

如需手工在开发机直装（不推荐用于生产 NAS）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo bash deploy/install.sh
```

还原 / 卸载：`sudo bash deploy/uninstall.sh`（加 `--purge` 连运行配置 `state/` 一起删，不影响官方数据）。

## 使用

- 打开飞牛影视，搜索任意**中文片名**（拼音上屏后才触发在线搜索，纯拼音碎片只走本地）。
- 标题带 **【在线】** 前缀、系列列显示「🌐在线·年份·地区」的卡片，点开即播；在线结果默认排在最前。
- 管理页（同源）：`https://<你的飞牛地址>/v/online-admin`
  - **资源站管理**：启用/停用、添加自定义苹果 CMS JSON 接口、删除（误删可用「恢复默认」还原出厂线路）
  - **连通性测试**：单源测试或「全部测速」，显示耗时、命中条数、直链数，方便筛掉失效源
  - 每词注入条数、在线前排开关
  - **噪音关键词自定义**：标签式增删、批量粘贴（逗号/顿号/换行分隔）、一键恢复默认；
    标题或备注命中任一关键词即不注入
  - **热门榜单媒体库**：开关、榜单刷新间隔、立即刷新；查看最近刷新时间、数据源成功数、
    各库条数与错误信息；展示豆瓣电影/剧集/动漫/综艺四个榜单，点击按需搜源播放

## 资源站接口格式

添加的自定义源需为苹果 CMS V10 标准 JSON 接口，形如：

```
https://站点/api.php/provide/vod/at/json        # 搜索 ?ac=detail&wd=关键词
```

返回 `{"code":1,"list":[{vod_name, vod_play_url, ...}]}` 即被识别。

## 管理 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v/api/ext/status` | 运行状态 + 配置（不含 token）|
| GET | `/v/api/ext/online?q=&limit=` | 已缓存在线条目 |
| POST | `/v/api/ext/config` | 保存设置/源启用状态（热生效，清搜索缓存）|
| POST | `/v/api/ext/junk-keywords` | `{keywords}` 自定义噪音词（自动分隔去重）或 `{reset:true}` 恢复默认 |
| POST | `/v/api/ext/source/add` | `{name,api}` 添加自定义源（按地址去重）|
| POST | `/v/api/ext/source/delete` | `{id}` 删除/隐藏一条线路（默认线路可恢复）|
| POST | `/v/api/ext/source/restore` | 恢复全部默认线路 |
| POST | `/v/api/ext/source/test` | `{id}` 或 `{api}` 单源连通性探测 |
| POST | `/v/api/ext/source/test-all` | 并发探测全部源 |
| POST | `/v/api/ext/hot/refresh` | 立即刷新豆瓣榜单缓存（失败保留旧缓存）|
| GET | `/v/online-admin` | 管理页 |

## 选集（原生 TV UI）

- 单集条目渲染为 **Movie**（点开即播）；多集条目渲染为 **TV**。
- TV 走飞牛原生选集接口：`GET /season/list/:tvGuid` 返回一个伪造 Season，
  `GET /episode/list/:seasonGuid` 返回每一集（确定性 `episode_guid=md5(ep:on:idx)`，
  集身份与播放线路解耦，切线路不换集）。
- 点某集 → `play/info{item_guid:episodeGuid}` → `play/play`，media/video guid 与该集绑定，
  播放器拉到的就是该集 HLS。媒体 guid 反查索引在 store 内存中维护，随搜索结果增量重建。

## 多线路切换

- 搜索时把**不同资源站命中的同一部片聚合成一个条目**（片名规范化 + 年份归并），
  每个资源站各成一条播放线路（线路键 `provider::组名`，同一站直链优先于 share 页，
  直链最多的线路为默认）。
- 播放设置里的**清晰度菜单即「线路」菜单**：`POST /stream` 与 `GET /stream/list`
  返回的 `qualities` 为每个可用线路生成一项（线路 1、线路 2……，不内置任何站点名称展示）。播放器切换该项时
  会重新调用 `POST /play/play`，代理用请求里的线路码（编码在 `bitrate` 字段）解析
  对应源，返回只含该线路的播放列表，从而在卡顿/失效时即时换源。
- 标题播放键对 TV 也可直接点击（`can_play=1`）：从未看过播第 1 集；有未看完的集则
  续播该集并恢复到上次秒数；已看完的集之后自动播下一集。
- 嵌套 master（源 `index.m3u8` 再套一层子 m3u8）会自动跟随解析（最多 4 层），
  并正确重写 `EXT-X-KEY`（AES）与 `EXT-X-MAP`（fMP4 初始化段）。
- `/share/<id>` 等播放页源在**播放时按需解析**（页面内嵌 `const url=...m3u8` 提取，
  30 分钟缓存），不拖慢搜索；直链线路始终优先于仅可解析的线路。

## 播放记录 / 继续观看

- 在线播放进度（`POST /play/record`，播放器高频上报）落在本地 `state/history.json`，
  不上报官方库。
- 代理 `GET /play/list` 时把在线历史（带海报、剧名、季集、`ts` 进度、`duration`）
  与官方本地历史合并返回，因此在线条目出现在「继续观看」，置顶按最近播放排序；
  进度 ≥95% 视为看完不再占位，`DELETE /play/record` 同步删除。
- `POST /play/info` 回填已记录的 `ts`（秒），播放器据此从上次位置续播。

## 打成 FPK 应用包

```
bash packaging/fpk/build.sh          # 产出 dist/fnmedia-ext-<ver>.fpk
```

- `deploy/fnmedia-ext.service.in` 是 systemd 模板，安装时 `deploy/install.sh` 按
  实际落盘路径渲染绝对路径，因此同一包可装到任意目录。
- `cmd/install_callback` 在首次安装时创建 venv 并 `pip install fastapi uvicorn httpx`，
  随后调用 `deploy/install.sh` 接管；`uninstall_callback` 调用 `deploy/uninstall.sh` 还原。

## 已知限制 / 路线图

- `/share/<id>` 解析器目前覆盖内嵌 `const/var url` / `{"url":...}` / 明文 m3u8
  的播放页（苹果系常见）；个别用加密 JS 动态拉流的页面可能解析不到（自动跳过该线路）。
- 多线路聚合依赖「片名规范化 + 年份」判定，极少数同名不同作品理论上可能被并为一条。
- 已在 fnOS 应用中心完成全新安装 / 停用 / 启用验证；适配版本以本文顶部「适配版本」表为准。
  官方 trim.media 升级若改动签名常量或接口结构，需要相应更新本扩展；停用即可恢复官方服务。

## ☕ 为爱发电 · 欢迎打赏

本项目开源、免费、无广告，所有功能均利用业余时间开发与维护。如果它让你的飞牛影视
更好用，欢迎扫码请作者喝杯咖啡——你的支持是持续适配新设备、维护源解析和修复问题的动力。

<p align="center">
  <img src="proxy/assets/donate.jpg" alt="微信 / 支付宝 打赏收款码" width="220">
</p>

<p align="center">微信 / 支付宝 扫码，金额随意 ❤️ · 详见 <a href="DONATE.md">打赏说明</a></p>

打赏完全自愿，不影响任何功能使用，也不对应任何功能承诺或技术支持 SLA。
