#!/usr/bin/env python3
"""飞牛影视·在线扩展 管理台（单文件 HTML，无外部依赖）。

布局对齐 fnmusic-ext 管理台：桌面左侧导航 + 移动端底部导航，多页卡片。
完整版专属区块用可剥离标记包裹，公开构建会被打包脚本整块移除。
"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>fnmedia-ext 管理台</title>
<style>
:root{
  --bg:#f3f5f9;--card:#fff;--line:#e6eaf0;--text:#1c2733;--muted:#68788a;
  --accent:#2f6fed;--accent-soft:#e8f0fe;--ok:#1a9e5c;--warn:#d97706;
  --err:#d64545;--radius:14px;--shadow:0 1px 2px rgba(16,30,54,.04),0 4px 14px rgba(16,30,54,.06);
  --shadow-hover:0 10px 30px rgba(16,30,54,.12);--ease:cubic-bezier(.4,0,.2,1);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  background:var(--bg);color:var(--text);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.layout{display:flex;min-height:100vh;min-height:100dvh}

/* ---------- 侧栏 ---------- */
.sidebar{width:232px;flex:none;padding:22px 14px;background:var(--card);
  border-right:1px solid var(--line);display:flex;flex-direction:column;gap:16px;
  position:sticky;top:0;height:100vh;height:100dvh}
.brand{display:flex;align-items:center;gap:11px;padding:0 8px;flex:none}
.brand-mark{width:42px;height:42px;border-radius:12px;flex:none;display:flex;
  align-items:center;justify-content:center;font-size:22px;background:var(--accent-soft);
  box-shadow:0 2px 8px rgba(47,111,237,.16)}
.brand-name{font-weight:700;font-size:16px}
.brand-sub{font-size:12px;color:var(--muted)}
.nav{display:flex;flex-direction:column;gap:4px;flex:1 1 auto;min-height:0;
  overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;
  padding-right:4px;margin-right:-4px}
.nav-group-label{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);padding:14px 12px 5px;opacity:.75}
.nav-group-label:first-child{padding-top:2px}
.nav-item{position:relative;text-align:left;padding:10px 12px;border:0;border-radius:8px;
  background:transparent;color:var(--text);font-size:14.5px;cursor:pointer;
  transition:background .16s cubic-bezier(.4,0,.2,1),color .16s cubic-bezier(.4,0,.2,1)}
.nav-item:hover{background:var(--accent-soft)}
.nav-item.active{background:var(--accent);color:#fff;
  box-shadow:0 4px 12px rgba(47,111,237,.28);font-weight:600}
.sidebar-foot{margin-top:auto;font-size:12px;color:var(--muted);padding:0 8px;flex:none}

/* ---------- 主区 ---------- */
.main{flex:1;min-width:0;width:100%;padding:28px 32px 96px;max-width:960px}
.page{display:none}.page.active{display:block;animation:fadeIn .22s var(--ease)}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
h2.page-title{margin:4px 0 4px;font-size:22px;letter-spacing:-.01em}
.page-desc{color:var(--muted);font-size:13px;margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px 20px;margin-bottom:14px;box-shadow:var(--shadow);
  transition:box-shadow .2s var(--ease),border-color .2s var(--ease)}
.card:hover{box-shadow:var(--shadow-hover)}
.card-title{font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:15px}
.card-sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.hint{color:var(--muted);font-size:13px;margin:6px 0}
.muted{color:var(--muted)}.sm{font-size:12px}
.stack{display:flex;flex-direction:column;gap:8px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.mt{margin-top:10px}

/* ---------- 控件 ---------- */
input[type=text],input[type=number],input[type=password],textarea,select{
  background:#fff;border:1px solid var(--line);border-radius:9px;color:var(--text);
  padding:9px 11px;width:100%;font:inherit;
  transition:border-color .16s var(--ease),box-shadow .16s var(--ease)}
input[type=number]{width:auto}
textarea{resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
input:hover,textarea:hover,select:hover{border-color:#b9c7dc}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(47,111,237,.12)}
input:disabled,textarea:disabled,select:disabled{background:#f3f5f8;color:var(--muted);cursor:not-allowed}
label.field{display:block;margin:10px 0}
label.field>span{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}
.switch-row{display:flex;align-items:flex-start;gap:10px;padding:10px 0;cursor:pointer}
.switch-row input{width:17px;height:17px;margin-top:2px;accent-color:var(--accent);flex:none}
.switch-row .sub{display:block;font-size:13px;color:var(--muted)}
/* ---------- 统一按钮体系（对齐272）：实际HTML多使用button.primary.small，
   不能只给.btn写样式，否则按钮会退回浏览器默认外观。 ---------- */
.btn,main button,.main button{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--accent);background:#fff;color:var(--accent);
  border-radius:9px;padding:8px 15px;min-height:36px;cursor:pointer;
  font-family:inherit;font-size:13.5px;font-weight:500;line-height:1.2;
  white-space:nowrap;user-select:none;
  transition:background .16s var(--ease),border-color .16s var(--ease),
    color .16s var(--ease),box-shadow .16s var(--ease),transform .08s var(--ease)}
.btn:hover,main button:hover,.main button:hover{background:var(--accent-soft)}
.btn:active,main button:active,.main button:active{transform:translateY(1px)}
.btn:focus-visible,main button:focus-visible,.main button:focus-visible{
  outline:2px solid rgba(47,111,237,.28);outline-offset:2px}
.btn.primary,button.primary{background:var(--accent);border-color:var(--accent);color:#fff;
  box-shadow:0 2px 6px rgba(47,111,237,.16)}
.btn.primary:hover,button.primary:hover{background:var(--accent);color:#fff;opacity:.94;
  box-shadow:0 4px 12px rgba(47,111,237,.24)}
.btn.success,button.success{background:var(--ok);border-color:var(--ok);color:#fff}
.btn.success:hover,button.success:hover{background:var(--ok);color:#fff;opacity:.92}
.btn.danger,button.danger{background:#fff;border-color:var(--err);color:var(--err)}
.btn.danger:hover,button.danger:hover{background:var(--err);border-color:var(--err);color:#fff}
.btn.warn,button.warn{background:#fff;border-color:var(--warn);color:var(--warn)}
.btn.warn:hover,button.warn:hover{background:var(--warn);border-color:var(--warn);color:#fff}
.btn.ghost,button.ghost{border-color:var(--line);color:var(--muted);background:#fff}
.btn.ghost:hover,button.ghost:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.small,.btn.small{padding:5px 12px;font-size:12.5px;min-height:30px;border-radius:7px}
.btn:disabled,button:disabled{opacity:.45;cursor:not-allowed;background:#fff;color:var(--muted);
  border-color:var(--line);box-shadow:none;transform:none}
button.primary:disabled{background:var(--accent);color:#fff;opacity:.45}
.btn-row{display:flex;gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap}
label.btn,label.btn-sm{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--accent);background:#fff;color:var(--accent);border-radius:9px;
  padding:8px 15px;font-size:13.5px;cursor:pointer;white-space:nowrap;font-family:inherit}
label.btn-sm{padding:5px 12px;font-size:12.5px;border-radius:7px}
label.btn:hover,label.btn-sm:hover{background:var(--accent-soft)}

/* ---------- 徽标/状态点 ---------- */
.badge{font-size:11px;padding:1px 8px;border-radius:99px;background:var(--accent-soft);color:var(--accent);font-weight:500}
.dot{display:inline-flex;align-items:center;gap:6px;font-size:13px}
.dot::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--muted)}
.dot.ok::before{background:var(--ok)}.dot.bad::before{background:var(--err)}.dot.wait::before{background:var(--warn)}
.pill{font-size:11px;padding:2px 9px;border-radius:99px;white-space:nowrap;border:1px solid transparent}
.pill.on{color:var(--ok);background:#e7f6ee;border-color:#bfe6d0}
.pill.off{color:var(--muted);background:#eef1f5;border-color:var(--line)}
.pill.warn{color:#b45309;background:#fef3e2;border-color:#f3d9b0}
.chip{display:inline-flex;align-items:center;gap:5px;background:#eef1f5;border:1px solid var(--line);
  color:var(--text);border-radius:99px;padding:2px 6px 2px 11px;font-size:12px;margin:2px}
.chip b{cursor:pointer;font-weight:700;color:var(--muted);padding:0 3px;border-radius:99px}
.chip b:hover{color:var(--err)}
.tag{display:inline-block;font-size:10px;color:var(--accent);background:var(--accent-soft);
  border-radius:5px;padding:0 5px;margin-left:6px}
.tag.amber{color:#b45309;background:#fef3e2}
.tag.warn{color:#b45309;background:#fef3e2}
/* 统一订阅导入结果 */
.subgrp{border:1px solid var(--line);border-radius:9px;padding:8px 10px;margin:10px 0;background:#fbfcfe}
.subgrp-t{font-size:13px;font-weight:600;margin-bottom:6px}
.subrow{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:13px;padding:3px 0}
.subrow.ok span:first-child{color:var(--ok,#1a8d3a)}
.subrow.muted{color:var(--muted)}
.subgrp.android-dep{border-style:dashed;background:#f6f7f9}
.subgrp.android-dep .subgrp-t,.subrow.android-dep{color:var(--muted)}
.subrow.android-dep{opacity:.72}
tr.android-dep{background:#f6f7f9;opacity:.62}
.tag.off{color:var(--muted);background:#e9edf2}

/* ---------- 实时播放诊断 ---------- */
.live-dev{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0;background:#fbfcfe}
.live-rows{margin-top:6px;display:flex;flex-direction:column;gap:3px}
.live-row{display:flex;align-items:center;gap:6px;font-size:12px;flex-wrap:wrap}
.live-row .muted{font-size:11px}
.live-dev details{margin-top:6px}
.diag{background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px}

/* ---------- 表格 ---------- */
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.02em;background:#fafbfc}
tbody tr{transition:background .14s var(--ease)}
tbody tr:hover{background:var(--accent-soft)}
tr:last-child td{border-bottom:0}
.meta{color:var(--muted);font-size:12px;word-break:break-all}
.src-detail>summary{cursor:pointer;list-style:none;font-weight:500;outline:none;border-radius:6px}
.src-detail>summary:focus-visible{outline:2px solid rgba(47,111,237,.28);outline-offset:3px}
.src-detail>summary::-webkit-details-marker{display:none}
.src-detail>summary::before{content:"▸";display:inline-block;margin-right:5px;color:var(--muted);transition:transform .15s var(--ease)}
.src-detail[open]>summary::before{transform:rotate(90deg)}
.src-detail>summary:hover{color:var(--accent)}
.src-detail .src-detail-body{margin:7px 0 2px 18px;padding:7px 9px;background:#fafbfc;border:1px solid var(--line);border-radius:7px}
/* 长资源列表折叠、缓存卡片与源编辑弹窗（对齐272） */
.collapse-bar{display:flex;align-items:center;justify-content:center;gap:8px;cursor:pointer;user-select:none;
  font-size:12.5px;color:var(--muted);padding:9px 4px 4px;border-top:1px dashed var(--line);margin-top:10px}
.collapse-bar:hover{color:var(--accent)}
.collapse-bar .chev{transition:transform .18s;display:inline-block}
.collapse-bar.collapsed .chev{transform:rotate(-90deg)}
.longbox{max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.longbox.collapsed{max-height:170px}
.cache-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin:10px 0}
.cache-item{border:1px solid var(--line);border-radius:10px;padding:11px 12px;background:#fafbfc}
.cache-item .cn{font-size:13px;font-weight:600}
.cache-item .cv{font-size:12px;color:var(--muted);margin:3px 0 9px;word-break:break-all}
.modal-mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;
  justify-content:center;z-index:999;padding:18px}
.modal-mask.show{display:flex}
.modal{background:var(--card);border-radius:14px;width:min(520px,94vw);max-height:88vh;overflow:auto;
  box-shadow:0 18px 50px rgba(0,0,0,.25);padding:18px 20px}
.modal h3{margin:0 0 14px;font-size:16px}
.modal label.fld{display:block;font-size:12.5px;color:var(--muted);margin:10px 0 4px}
.modal .modal-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#f1f3f6;flex-wrap:wrap}
.seg button{border:none!important;background:transparent!important;padding:7px 14px;font-size:13px;color:var(--muted)!important;border-radius:0!important;box-shadow:none!important}
.seg button.active{background:var(--accent)!important;color:#fff!important;font-weight:600}
.del{color:var(--err);background:transparent;border:0;cursor:pointer;font-size:13px;padding:4px 6px}
.del:hover{background:#fee2e2!important;color:var(--err)!important}
.st{font-size:12px;white-space:nowrap}.st.ok{color:var(--ok)}.st.bad{color:var(--err)}.st.wait{color:var(--muted)}

/* ---------- 概览统计 ---------- */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:#fafbfc;border:1px solid var(--line);border-radius:12px;padding:14px 16px;
  transition:transform .16s var(--ease),box-shadow .16s var(--ease)}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-hover)}
.stat .n{font-size:22px;font-weight:700}
.stat .l{font-size:12px;color:var(--muted)}
.lib-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.lib{display:flex;align-items:center;gap:10px;background:#fafbfc;border:1px solid var(--line);
  border-radius:10px;padding:10px 12px}
.lib .ic{font-size:20px}
.lib .t{font-weight:600;font-size:14px}
.lib .c{font-size:12px;color:var(--muted)}
.lib .pin{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);
  cursor:pointer;white-space:nowrap;padding:2px 8px;border-radius:99px;border:1px solid var(--line);background:#fff}
.lib .pin input{margin:0;accent-color:var(--accent)}
.lib:has(.pin input:checked){border-color:var(--accent);background:var(--accent-soft)}

/* ---------- 优先级 ---------- */
.prio-input{width:74px}
.prio-bar{height:5px;border-radius:3px;background:linear-gradient(90deg,#cfe0fb,#2f6fed);margin-top:3px}

/* ---------- 弹窗/日志/toast ---------- */
pre.log{max-height:240px;overflow:auto;background:#0f1115;color:#d6dae0;border-radius:8px;
  padding:10px;font-size:11px;line-height:1.5;margin:6px 0 0;white-space:pre-wrap}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);
  background:#222;color:#fff;padding:9px 18px;border-radius:99px;font-size:13px;opacity:0;
  box-shadow:0 8px 24px rgba(16,30,54,.18);transition:.22s;pointer-events:none;z-index:80;max-width:90vw}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.ok{background:var(--ok)}#toast.fail{background:var(--err)}

/* ---------- 移动端 ---------- */
.bottom-nav,.more-menu{display:none}
@media(max-width:760px){
  .sidebar{display:none}
  .main{padding:max(16px,env(safe-area-inset-top,0px)) 14px
    calc(64px + env(safe-area-inset-bottom,8px));max-width:100%}
  h2.page-title{font-size:18px}
  input[type=text],input[type=password],textarea,select{font-size:16px}
  main button:not(.small){min-height:42px}
  main button.small{min-height:36px}
  .bottom-nav{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:45;
    height:calc(58px + env(safe-area-inset-bottom,0px));box-sizing:border-box;align-items:center;
    background:rgba(255,255,255,.97);border-top:1px solid var(--line);backdrop-filter:blur(6px);
    padding-bottom:env(safe-area-inset-bottom,0px)}
  .bottom-nav .nav-item{flex:1;display:flex;align-items:center;justify-content:center;
    height:58px;text-align:center;padding:0 2px;font-size:12px;border-radius:0;white-space:nowrap}
  .bottom-nav .nav-item.active{background:transparent;color:var(--accent);font-weight:700}
  .more-menu{position:fixed;left:10px;right:10px;bottom:calc(62px + env(safe-area-inset-bottom,0px));
    z-index:46;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;padding:10px;
    background:var(--card);border:1px solid var(--line);border-radius:14px;
    box-shadow:0 12px 36px rgba(16,30,54,.18);max-height:62vh;overflow-y:auto}
  .more-menu.show{display:grid}
  .more-menu .nav-item{min-height:42px;font-size:13px;border:1px solid var(--line);background:#fafbfc}
}
</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">🎬</div>
      <div><div class="brand-name">fnmedia-ext</div><div class="brand-sub">管理台</div></div>
    </div>
    <nav class="nav" id="nav">
      <div class="nav-group-label">总览</div>
      <button class="nav-item active" data-page="overview">📊 概览</button>

      <div class="nav-group-label">内容</div>
      <button class="nav-item" data-page="library">🎞 媒体库</button>

      <div class="nav-group-label">片源</div>
      <button class="nav-item" data-page="sources">📡 资源站</button>
      <button class="nav-item" data-page="importsub">📥 订阅与 JS 源</button>

      <div class="nav-group-label">播放与搜索</div>
      <button class="nav-item" data-page="playback">▶️ 播放设置</button>
      <button class="nav-item" data-page="search">🔍 搜索设置</button>

      <div class="nav-group-label">系统</div>
      <button class="nav-item" data-page="network">🌐 网络与代理</button>
      <button class="nav-item" data-page="data">🗄 数据与维护</button>
      <button class="nav-item" data-page="about">ℹ️ 关于与更新</button>
    </nav>
    <div class="sidebar-foot">
      <span id="globalState" class="dot wait">检测中…</span>
    </div>
  </aside>

  <main class="main">

    <!-- ============ 概览 ============ -->
    <section class="page active" id="page-overview">
      <h2 class="page-title">概览</h2>
      <p class="page-desc">在飞牛原生搜索与媒体库中注入在线影视，点击直接调用飞牛原生播放器。</p>

      <div class="card">
        <div class="card-title">当前能力</div>
        <p class="card-sub">统一无安卓版：苹果 CMS / T4 / DRPY JS 源、虚拟媒体库、网盘转存与小雅聚合。
          Guard 与普通 csp_ 不再导入；播放、续播、去广告与多线路策略保持原有基线。</p>
      </div>

      <div class="card">
        <div class="card-title">本设备识别 <span class="badge" id="ovClientKind">-</span></div>
        <div class="hint" id="ovClient">读取中…</div>
        <div class="muted sm" id="ovClientUa" style="word-break:break-all"></div>
      </div>

      <div class="card">
        <div class="card-title">实时播放诊断
          <span class="muted sm" style="font-weight:400">
            <span class="dot ok" id="livePulse" style="margin-left:8px"><span id="liveState">实时</span></span>
            <a href="#" id="liveClear" style="margin-left:10px">清空</a>
            <a href="#" id="liveCopy" style="margin-left:10px">📋 复制诊断信息</a>
          </span>
        </div>
        <div class="hint">显示各端（电视/手机/iOS/网页）最近 30 分钟的播放请求：走本地构造还是透传官方、当前在播、是否报错。每 3 秒刷新。</div>
        <div id="liveDiagnosis" style="margin:6px 0"></div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 10px" class="sm muted">
          <span>来源：<b style="color:var(--ok)">local</b> 插件构造</span>
          <span><b style="color:#2563eb">mixed</b> 合并官方</span>
          <span><b style="color:#b45309">upstream</b> 透传官方</span>
          <span><b style="color:var(--err)">4xx/5xx</b> 出错</span>
        </div>
        <div id="liveDevices"><div class="muted sm">暂无播放活动（在电视或手机上播一部片即可看到）</div></div>
      </div>

      <div class="card">
        <div class="card-title">统计</div>
        <div class="stat-grid">
          <div class="stat"><div class="n" id="ovSrcN">-</div><div class="l">启用资源站</div></div>
          <div class="stat"><div class="n" id="ovCacheN">-</div><div class="l">已缓存在线条目</div></div>
          <div class="stat"><div class="n" id="ovHotN">-</div><div class="l">热门榜单条目</div></div>
          <div class="stat"><div class="n" id="ovCatN">-</div><div class="l">海量库可用类目</div></div>
        </div>
        <div class="hint" id="ovHotMeta"></div>
      </div>

    </section>

    <!-- ============ 虚拟媒体库 ============ -->
    <section class="page" id="page-library">
      <h2 class="page-title">虚拟媒体库</h2>
      <p class="page-desc">插件在飞牛影视媒体库中虚拟出的内容。热门榜单与海量片库各自独立开关；每张库卡可单独「置顶」。</p>

      <div class="card">
        <div class="card-title">① 热门资源（各类目真实榜单）</div>
        <p class="card-sub">海报 / 评分 / 排名来自豆瓣榜单，后台定时刷新、打开秒读缓存；
          点进某部片时才用片名实时搜索资源站解析播放。点击播放时<strong>优先高质量源</strong>。</p>
        <div class="lib-grid" id="hotLibs"></div>
        <label class="switch-row">
          <input type="checkbox" id="hotEnabled"/>
          <span>在飞牛影视媒体库显示热门四库
            <span class="sub">🎬热门电影 · 📺热门剧集 · 🌸动漫新番 · 🎤热门综艺（独立开关，不影响海量四库）</span></span>
        </label>
        <label class="field" style="max-width:240px">
          <span>榜单自动刷新间隔（小时，1–24）</span>
          <input type="number" id="hotInterval" min="1" max="24"/>
        </label>
        <div class="hint" id="hotStatus">等待加载状态…</div>
        <div class="btn-row">
          <button class="primary small" onclick="saveHotCfg()">保存热门设置</button>
          <button class="small" onclick="refreshHot()">立即刷新榜单</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">② 海量片库（资源站分类深翻）</div>
        <p class="card-sub">直接按苹果 CMS 分类深翻资源站，跨源聚合成「🌐海量片库」四库（电影 / 剧集 / 动漫 / 综艺），规则不变。</p>
        <div class="lib-grid" id="catLibs"></div>
        <label class="switch-row">
          <input type="checkbox" id="catEnabled"/>
          <span>在飞牛影视媒体库显示海量四库
            <span class="sub">🎞海量电影 · 📺海量剧集 · 🌸海量动漫 · 🎤海量综艺（独立开关，不影响热门四库）</span></span>
        </label>
        <div class="btn-row">
          <button class="primary small" onclick="saveCatCfg()">保存海量设置</button>
        </div>
        <div class="hint">海量库在分类路由建立后自动出现；也可在「资源站」页把某个站单独生成一张媒体库卡片。</div>
      </div>

      <div class="card">
        <div class="card-title">③ T4 选片</div>
        <div class="lib-grid" id="t4Libs"></div>
        <label class="switch-row">
          <input type="checkbox" id="t4Enabled" onchange="saveT4Cfg()"/>
          <span>在飞牛影视媒体库显示 T4 选片</span>
        </label>
      </div>


      <div class="card">
        <div class="card-title">⑤ 媒体库显示开关与排序</div>
        <p class="card-sub">勾选=在飞牛影视里显示；取消=隐藏（对所有账号不显示，可随时恢复）。
          ↑/↓ 调整虚拟媒体库之间的先后顺序；下方选择整体放在本地媒体库之前还是之后。</p>
        <div class="btn-row" style="margin-bottom:8px">
          <label class="field" style="max-width:260px"><span>虚拟媒体库整体位置</span>
            <select id="libPlacement" onchange="onPlacementChange()">
              <option value="front">放在本地媒体库之前</option>
              <option value="back">放在本地媒体库之后</option>
              <option value="after">插到指定本地库之后…</option>
            </select></label>
          <label class="field" id="anchorWrap" style="max-width:260px;display:none"><span>锚定的本地媒体库</span>
            <select id="libAnchor" onchange="savePlacement()"></select></label>
        </div>
        <div id="libManage" style="display:grid;gap:6px;max-height:340px;overflow:auto"></div>
      </div>

      <div class="card">
        <div class="card-title">⑥ 子账号观看权限（家庭多用户）</div>
        <p class="card-sub">飞牛影视支持多账号登录。可为孩子/家人账号单独勾选可见媒体库；未单独配置的账号按下方默认策略。
          被限制的账号搜索时只出本地结果，不会误点播在线片源；<b>各账号的在线播放记录与续播进度彼此隔离</b>。</p>
        <label class="switch-row">
          <input type="checkbox" id="aclDefaultVisible"/>
          <span>未配置的账号默认可见所有虚拟媒体库
            <span class="sub">关闭后，未单独授权的账号只能看到飞牛本地媒体库</span></span>
        </label>
        <label class="switch-row">
          <input type="checkbox" id="aclDefaultOnline"/>
          <span>未配置的账号默认可用在线搜索/在线片源</span>
        </label>
        <div class="btn-row">
          <button class="primary small" onclick="saveAclDefaults()">保存默认策略</button>
        </div>
        <div class="hint" style="margin:6px 0">开启「单独授权」后勾选该账号允许观看的媒体库；「在线搜索/片源」可独立控制：</div>
        <div id="aclBox" class="stack"></div>
      </div>

    </section>

    <!-- ============ 资源站 ============ -->
    <section class="page" id="page-sources">
      <h2 class="page-title">资源站</h2>
      <p class="page-desc">标准苹果 CMS 源与扩展源；启用后进入聚合搜索与海量片库。</p>
      <div class="card">
        <div class="card-title">资源站列表 <span class="badge" id="srcCount"></span>
          <input type="text" id="srcFilter" placeholder="筛选名称/地址…" oninput="renderSources()"
                 style="margin-left:auto;max-width:168px;font-size:12px;padding:5px 9px"/>
          <button class="small" onclick="testAll()">全部测速</button>
          <button class="small" id="srcProbeBtn" onclick="sourceProbeStart()">批量探活</button>
          <button class="small ghost" id="srcProbeCancelBtn" style="display:none" onclick="sourceProbeCancel()">中止</button>
          <button class="small ghost" onclick="restoreBuiltin()">恢复内置源</button>
          <button class="small" onclick="exportCmsSources()" title="导出当前纯 CMS 源为 JSON 备份">⬇ 导出</button>
          <label class="btn-sm" style="border-color:var(--ok);background:var(--ok);color:#fff" title="从 JSON 文件导入 CMS 源，自动去重">
            ⬆ 导入<input type="file" id="cmsImportFile" accept=".json,application/json" style="display:none" onchange="importCmsFile(this)"/>
          </label>
        </div>
        <div style="overflow-x:auto">
        <table>
          <thead><tr><th style="width:42px">启用</th><th style="width:96px">选项</th><th>资源站</th>
            <th style="width:150px">连通性</th><th style="width:70px">测试</th><th style="width:44px"></th><th style="width:44px"></th></tr></thead>
          <tbody id="srcRows"><tr><td colspan="7" class="muted">加载中…</td></tr></tbody>
        </table>
        </div>
        <div id="srcProbeProgress" class="hint" style="display:none"></div>
        <div class="collapse-bar collapsed" id="srcCollapseBar" onclick="toggleSrcCollapse()">
          <span class="chev">▾</span><span id="srcCollapseTxt">展开完整列表</span></div>
        <div class="row mt" style="display:grid;grid-template-columns:150px 1fr 90px auto;gap:10px" id="addGrid">
          <input type="text" id="addName" placeholder="名称（可选）"/>
          <input type="text" id="addApi" placeholder="苹果CMS JSON 接口，如 https://xxx.com/api.php/provide/vod/at/json"/>
          <label style="display:flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">
            <input type="checkbox" id="addStandalone" style="width:15px;height:15px;accent-color:var(--accent)"/>独立库</label>
          <button class="primary small" style="justify-content:center" onclick="addSource()">添加</button>
        </div>
        <div class="hint">「独立库」= 该站单独生成一张媒体库卡片；「非过滤」= 不按标题/备注过滤解说/预告/花絮；
          删除内置源仅隐藏，可恢复。批量导入 TVBox 订阅请用左侧「导入订阅」。</div>
      </div>

      <div class="card">
        <div class="card-title">片源质量策略</div>
        <p class="card-sub">热门资源点击搜源时，按「源质量权重 → 可播直链数」排序，优先选择高质量源。</p>
        <label class="switch-row">
          <input type="checkbox" id="preferQ" checked/>
          <span>热门资源优先高质量源
            <span class="sub">关闭后只按可播直链总数选择来源</span></span>
        </label>
        <div class="btn-row"><button class="primary small" onclick="savePreferQ()">保存</button></div>
      </div>
      <div class="card">
        <div class="card-title">各源质量权重 <span class="badge" id="prioCount"></span>
          <button class="small" style="margin-left:auto" onclick="resetPriority()">恢复默认权重</button>
        </div>
        <p class="card-sub">数值越大越优先（-100–100，留空/0 用内省默认）。历史 Guard/csp 仅只读展示，不参与有效源策略。</p>
        <table>
          <thead><tr><th>资源站</th><th style="width:120px">质量权重</th><th>相对优先级</th></tr></thead>
          <tbody id="prioRows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
        </table>
        <div class="btn-row"><button class="primary small" onclick="savePriority()">保存权重</button></div>
      </div>

      <div class="card">
        <div class="card-title">源健康画像
          <span class="hint" style="font-weight:400">按真实播放探测统计成功率/延迟，死源自动降权 15 分钟</span>
          <button class="small" style="margin-left:auto" onclick="loadHealth()">刷新</button>
        </div>
        <table>
          <thead><tr><th>源</th><th style="width:80px">样本</th>
            <th style="width:110px">成功率</th><th style="width:100px">平均延迟</th>
            <th style="width:90px">状态</th><th style="width:70px">操作</th></tr></thead>
          <tbody id="healthRows"><tr><td colspan="6" class="muted">播放几次后自动生成。</td></tr></tbody>
        </table>
      </div>
    </section>

    <!-- ============ 导入 TVBox 订阅（独立页） ============ -->
    <section class="page" id="page-importsub">
      <h2 class="page-title">订阅与 JS 源</h2>
      <p class="page-desc">导入 TVBox / 影视仓 / OK影视 订阅，统一管理远程 DRPY JavaScript 源；支持苹果 CMS、T4 与 DRPY，Guard/普通 csp_ 仅报告不导入。</p>
      <div class="card">
        <div class="card-title">TVBox / 影视仓 / OK影视 订阅</div>
        <p class="card-sub">自动识别并分类：标准苹果CMS影视站（type 0/1）、T4 选片源
          能直接播放的<strong>自动启用</strong>；DRPY 脚本暂未就绪的源导入后默认关闭，可稍后重试；
          Guard/普通 csp_ 等安卓依赖源仅列出识别结果、不导入；网盘/直播/无法识别的源跳过并说明原因。</p>
        <div class="row"><input type="text" id="subUrl" placeholder="配置订阅 URL（支持 base64 与多订阅聚合）" style="flex:2"/>
          <button class="primary small" onclick="importSubscription('url')">按 URL 导入</button>
        </div>
        <label class="field"><textarea id="subText" rows="7" placeholder="或直接粘贴配置 JSON（含 sites 数组）…"></textarea></label>
        <div class="btn-row">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="subStandalone" style="width:15px;height:15px;accent-color:var(--accent)"/>
            苹果CMS源同时「作为独立媒体库」</label>
          <label style="display:flex;align-items:center;gap:5px;font-size:12px">自动刷新
            <input type="number" id="subRefreshDays" value="1" min="0" max="365" title="0=纯手动；1–365 天，连续失败 3 次暂停" style="width:58px"/> 天
          </label>
          <button class="primary small" onclick="importSubscription('text')">解析并导入</button>
        </div>
        <div id="subResult"></div>
      </div>


    </section>


    <!-- ============ 播放设置 ============ -->
    <section class="page" id="page-playback">
      <h2 class="page-title">播放设置</h2>
      <p class="page-desc">仅管理播放链路：线路选择、坏线切换、分片直连与去广告；不改变订阅、源站或媒体库配置。</p>
      <div class="card">
        <div class="card-title">播放线路</div>
        <label class="switch-row"><input type="checkbox" id="lineFastest"/>
          <span>自动选最快线路<span class="sub">起播时并行测速、自动选首字节最快的线路；同一集短时间内固定用它</span></span></label>
        <label class="field" style="max-width:260px"><span>参与测速的线路数</span>
          <input type="number" id="fastestMax" min="2" max="12" style="width:100%"/></label>
        <label class="field" style="max-width:260px"><span>测速超时（秒）</span>
          <input type="number" id="fastestTimeout" min="1" max="10" step="0.5" style="width:100%"/></label>
        <label class="switch-row"><input type="checkbox" id="lineAutofail"/>
          <span>坏线自动切换<span class="sub">当前线路失效/文件不存在时自动换到可用线路，并短时跳过坏线（关闭则纯手动切线）</span></span></label>
        <label class="switch-row"><input type="checkbox" id="directPlay"/>
          <span>混合直连（视频分片不经 NAS 中转）<span class="sub">仅对验证可裸连的普通 HLS 直放线生效，播放端直连源站分片以省 NAS 带宽/负载；加密、去广告、解析线、网盘等自动回退中转。默认关闭。</span></span></label>
        <div class="btn-row"><button class="primary small" onclick="saveLines()">保存播放设置</button></div>
      </div>
      <div class="card">
        <div class="card-title">🛡 播放去广告（m3u8 流层）
          <label style="float:right;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:400;cursor:pointer">
            <input type="checkbox" id="adfEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="toggleAdfilter(this.checked)"/> 启用去广告</label></div>
        <p class="card-sub">播放时实时检测当前线路并去除广告，兼容 TVBox / OK影视 Pro：<strong>① 动态检测</strong>——按分片间断边界、切片时长模式、CDN 切换、片头/片中位置实时识别广告段（无需订阅自带规则）；<strong>② 订阅 rules</strong>——导入订阅时自动收录其去广告正则，按线路域名精准剥除；<strong>③ 选集过滤</strong>——剔除“关注公众号/加微信/下载APP”假选集。识别不确定或可能误伤时自动保留，不影响播放。</p>
        <div style="margin:6px 0">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="adfDynamic" style="width:14px;height:14px;accent-color:var(--accent)" onchange="toggleAdfilterDynamic(this.checked)"/>
            播放时动态检测（识别订阅未提供规则的广告段）</label>
        </div>
        <div id="adfStatus" class="muted sm" style="margin-top:6px">正在加载去广告规则…</div>
      </div>
    </section>

    <!-- ============ 搜索设置 ============ -->
    <section class="page" id="page-search">
      <h2 class="page-title">搜索设置</h2>
      <div class="card">
        <label class="switch-row"><input type="checkbox" id="onlineFirst"/>
          <span>在线资源结果前排展示<span class="sub">在线片库更齐全；关闭则本地优先</span></span></label>
        <label class="field" style="max-width:240px"><span>每个关键词最多注入条数</span>
          <input type="number" id="maxn" min="1" max="50" style="width:100%"/></label>
        <label class="switch-row"><input type="checkbox" id="junk"/>
          <span>按关键词过滤噪音<span class="sub">标题或备注命中噪音词则不注入</span></span></label>
        <label class="field"><span>噪音关键词 <span id="kwN" class="muted"></span>
          <span style="float:right">
            <button class="small" type="button" onclick="resetKw()">恢复默认</button>
            <button class="small" type="button" onclick="toggleKwEdit()">批量编辑</button>
          </span></span>
          <div id="kwChips" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px"></div>
          <div id="kwEdit" style="display:none;margin-bottom:8px">
            <textarea id="kwArea" rows="4" placeholder="每行一个，或逗号/顿号分隔，如：解说,预告片,花絮,枪版"></textarea>
            <button class="small" type="button" onclick="applyKwText()" style="margin-top:6px">应用文本</button>
          </div>
          <div class="row"><input type="text" id="kwAdd" placeholder="添加关键词后回车" style="flex:1"/>
            <button class="small" type="button" onclick="addKw()">添加</button></div>
        </label>
        <div class="btn-row"><button class="primary small" onclick="saveCfg()">保存设置</button></div>
      </div>

    </section>

    <!-- ============ 网络与代理 ============ -->
    <section class="page" id="page-network">
      <h2 class="page-title">网络与代理</h2>
      <p class="page-desc">三类代理分别服务订阅下载、资源站取源和版本更新，配置彼此独立；视频分片与播放策略不因此改变。</p>
      <div class="card">
        <div class="card-title">订阅抓取代理（可选）
          <label style="float:right;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:400;cursor:pointer">
            <input type="checkbox" id="subProxyEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="onSubProxyToggle()"/> 启用代理</label></div>
        <p class="card-sub">仅用于<strong>拉取订阅配置与 DRPY JavaScript 规则</strong>（GitHub 等被墙/解析失败时可走 Clash）。<strong>播放、搜索、CMS 探测一律不走此代理</strong>。形如 <code>http://用户:密码@主机:端口</code>，清空=直连。</p>
        <div class="row">
          <input type="text" id="subProxyUrl" placeholder="http://Clash:密码@192.168.x.x:7890" style="flex:2" disabled/>
          <button class="small" id="subProxySaveBtn" onclick="saveSubProxy(true)" disabled>保存并测试</button>
          <button class="small" id="subProxyClearBtn" onclick="clearSubProxy()" disabled>清空(直连)</button>
        </div>
        <div id="subProxyMsg" class="muted sm" style="margin-top:6px"></div>
      </div>
      <div class="card">
        <div class="card-title">🌐 资源站取源代理（可选 · 直连慢时）</div>
        <p class="card-sub">仅用于搜索、分类深翻、测速和播放地址解析；视频分片仍直连，不占代理带宽。留空即直连，不改变现有播放策略。</p>
        <div class="row">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
            <input type="checkbox" id="srcProxyEnable" style="width:14px;height:14px;accent-color:var(--accent)" onchange="onSrcProxyToggle()"/> 已配置代理</label>
        </div>
        <div class="row">
          <input type="text" id="srcProxyUrl" placeholder="http://Clash:密码@192.168.x.x:7890" style="flex:2" disabled/>
          <button class="small primary" id="srcProxySaveBtn" onclick="saveSrcProxy(true)" disabled>保存并测速</button>
          <button class="small ghost danger" id="srcProxyClearBtn" onclick="clearSrcProxy()" disabled>清空</button>
        </div>
        <div class="seg" id="srcProxyMode" style="margin-top:10px" role="tablist">
          <button type="button" data-mode="auto" onclick="setSrcProxyMode('auto')">🪄 智能（推荐）</button>
          <button type="button" data-mode="on" onclick="setSrcProxyMode('on')">始终走代理</button>
          <button type="button" data-mode="off" onclick="setSrcProxyMode('off')">全部直连</button>
        </div>
        <div class="hint" id="srcProxyModeHint" style="margin-top:6px"></div>
        <div id="srcProxyMsg" class="muted sm" style="margin-top:6px"></div>
      </div>
      <div class="card">
        <div class="card-title">版本更新代理（可选）</div>
        <p class="card-sub">仅用于检查版本与下载统一安装包；仅本机保存、不随安装包分发，保存后回「关于与更新」重新检查生效。</p>
        <div class="row">
          <input id="updProxy" type="text" class="input" placeholder="http://用户:密码@代理IP:端口（留空直连）" style="flex:2;min-width:240px">
          <button class="small primary" id="updProxySave">保存代理</button>
        </div>
      </div>
    </section>

    <!-- ============ 数据与维护 ============ -->
    <section class="page" id="page-data">
      <h2 class="page-title">数据与维护</h2>
      <p class="page-desc">查看在线条目缓存并维护搜索、榜单、分类与健康画像数据；缓存清理不改变源站或播放配置。</p>
      <div class="card">
        <div class="card-title">已缓存在线条目 <span class="badge" id="cacheN"></span></div>
        <div class="row"><input type="text" id="kw" placeholder="按标题过滤（在飞牛搜索过的会出现在这里）" style="flex:1"/>
          <button class="small" onclick="loadOnline()">刷新</button></div>
        <div class="longbox collapsed" id="onlineBox" style="margin-top:10px">
          <table>
            <thead><tr><th>标题</th><th style="width:100px">来源</th><th style="width:70px">集数</th></tr></thead>
            <tbody id="rows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
          </table>
        </div>
        <div class="collapse-bar collapsed" onclick="toggleBox('onlineBox',this)"><span class="chev">▾</span><span>展开/收起</span></div>
      </div>

      <div class="card">
        <div class="card-title">🗑 运行缓存管理
          <span class="hint" style="font-weight:400">缓存异常或占用过大时清除，后续访问自动重建</span>
          <button class="small" style="margin-left:auto" onclick="loadCacheStatus()">刷新</button>
        </div>
        <div id="cacheGrid" class="cache-grid"><div class="muted sm">读取中…</div></div>
        <div class="btn-row">
          <button class="small danger" onclick="clearCaches(['search'])">仅清搜索缓存</button>
          <button class="small danger" onclick="clearCaches(['charts','catalog'])">清榜单+海量库</button>
          <button class="small danger" onclick="clearCaches(['search','charts','catalog'])">一键清运行缓存（不重启）</button>
          <button class="small danger" onclick="clearCaches(['health'])">重置源健康画像</button>
          <button class="small danger" onclick="clearCaches(['titles'])">清在线资源缓存（将重启）</button>
        </div>
      </div>
    </section>

    <!-- ============ 关于与更新 ============ -->
    <section class="page" id="page-about">
      <h2 class="page-title">关于与更新</h2>
      <div class="card">
        <div class="card-title">🎬 fnmedia-ext · 飞牛影视在线扩展</div>
        <p class="card-sub">在原生搜索 / 媒体库注入在线影视并直接调用飞牛原生播放器播放。</p>
        <div class="stack sm muted">
          <div>· 热门资源：豆瓣真实榜单四库，点击搜源、优先高质量源播放。</div>
          <div>· 海量片库：苹果 CMS 分类深翻，跨源聚合成海量四库与单源独立库。</div>
        </div>
        <div class="hint" id="aboutEdition"></div>
      </div>

      <div class="card" id="updCard">
        <div class="card-title">🔄 版本更新</div>
        <div class="stack sm">
          <div class="muted">当前版本：<b id="updCur">…</b><span id="updEdition" class="muted"></span></div>
          <div id="updBody" class="muted">正在检查更新…</div>
          <div id="updActions" style="display:none;gap:8px;flex-wrap:wrap;align-items:center">
            <button class="btn primary" id="updBtn">立即更新</button>
            <button class="btn" id="updRefresh">重新检查</button>
            <a class="btn" id="updLink" target="_blank" rel="noopener" style="text-decoration:none">查看 Release</a>
          </div>
          <div id="updProgress" class="hint" style="display:none"></div>
        </div>
      </div>

      <div class="card" id="donateCard" style="text-align:center">
        <div class="card-title" style="justify-content:center">☕ 为爱发电 · 欢迎打赏</div>
        <p class="card-sub" style="max-width:560px;margin:0 auto 12px">
          本扩展<b>开源、免费、无广告</b>，所有功能都利用业余时间开发维护。
          如果它让你的飞牛影视更好用，你的一份打赏就是对作者最大的鼓励，
          也会让后续适配新设备、维护资源解析更有动力 🙏
        </p>
        <div style="display:flex;flex-wrap:wrap;gap:20px;justify-content:center;align-items:flex-start">
          <div style="display:inline-flex;flex-direction:column;align-items:center;gap:8px">
            <div style="padding:10px;border:1px solid var(--line);border-radius:12px;background:#fff;display:flex;align-items:center;justify-content:center;height:260px">
              <img src="/v/ext/alipay-qr" alt="支付宝打赏码"
                   style="width:200px;height:240px;object-fit:contain;display:block;border-radius:6px"
                   onerror="this.style.display='none'"/>
            </div>
            <div class="muted sm" style="font-weight:600">支付宝扫码打赏</div>
          </div>
          <div style="display:inline-flex;flex-direction:column;align-items:center;gap:8px">
            <div style="padding:10px;border:1px solid var(--line);border-radius:12px;background:#fff;display:flex;align-items:center;justify-content:center;height:260px">
              <img src="/v/ext/wechat-qr" alt="微信赞赏码"
                   style="width:176px;height:240px;object-fit:contain;display:block;border-radius:6px"
                   onerror="this.style.display='none'"/>
            </div>
            <div class="muted sm" style="font-weight:600">微信扫码赞赏</div>
          </div>
        </div>
        <div class="muted sm" style="margin-top:8px">金额随意，感谢支持 ❤️</div>
      </div>

      <div class="card">
        <div class="card-title">⚖️ 免责声明与版权声明</div>
        <div class="stack sm muted" style="line-height:1.7">
          <div><b>1. 工具性质：</b>本扩展是<b>开源、免费</b>的技术研究与个人媒体库整理工具，仅在你自己的 NAS / 设备上本地运行。它是一个中立的“播放器 / 接口聚合”技术框架，本身<b>不提供、不存储、不上传、不分发、不转码</b>任何影视、图片、字幕、播放地址或资源站点信息，也<b>不内置任何资源数据库、订阅、站点地址或第三方执行脚本</b>。</div>
          <div><b>2. 不内置源、由你自备：</b>所有在线内容均来自你<b>自行添加</b>的第三方接口、TVBox 订阅或 DRPY JavaScript 规则；是否添加、添加什么、是否启用，完全由你决定并自行承担。Guard/普通 csp_ 等安卓执行源不受支持。请仅使用你拥有合法授权或有权访问的内容与组件。</div>
          <div><b>3. 版权归原权利人：</b>第三方接口返回内容的版权归原权利人所有。本项目不依附、不代表任何内容提供方，不对任何第三方内容的合法性、准确性、可用性负责；如涉及版权问题，请联系内容实际提供方处理。</div>
          <div><b>4. 仅供学习交流：</b>本工具仅供学习、研究与个人合法播放自有或已获授权内容使用，<b>不得用于商业用途，不得下载、录制、再传播任何侵权内容</b>。请遵守你所在地的法律法规。</div>
          <div><b>5. 风险与责任：</b>第三方接口可能失效、变更或包含不当内容；使用过程中产生的流量、账号、费用与一切法律责任均由<b>使用者本人</b>承担，与本工具作者无关。请合法、自律使用，并妥善管理设备与访问权限，避免 18 周岁以下人员接触不适宜内容。</div>
          <div><b>6. 侵权处理：</b>如任何权利人认为本工具的某项技术实现涉及其合法权益，可通过项目仓库 Issue 联系作者，核实后将及时处理。</div>
        </div>
      </div>
    </section>
  </main>
</div>

<div class="more-menu" id="moreNav">
  <button class="nav-item" data-page="importsub">📥 订阅与 JS 源</button>
  <button class="nav-item" data-page="search">🔍 搜索设置</button>
  <button class="nav-item" data-page="network">🌐 网络与代理</button>
  <button class="nav-item" data-page="data">🗄 数据与维护</button>
  <button class="nav-item" data-page="about">ℹ️ 关于与更新</button>
</div>
<nav class="bottom-nav" id="bottomNav">
  <button class="nav-item active" data-page="overview">概览</button>
  <button class="nav-item" data-page="library">媒体库</button>
  <button class="nav-item" data-page="sources">片源</button>
  <button class="nav-item" data-page="playback">播放</button>
  <button class="nav-item" id="moreNavBtn" onclick="toggleMoreNav()">更多</button>
</nav>

<div id="toast"></div>

<!-- 资源站编辑弹窗 -->
<div class="modal-mask" id="srcEditMask" onclick="if(event.target===this)closeSrcEdit()">
  <div class="modal">
    <h3>✎ 编辑资源站</h3>
    <input type="hidden" id="seId"/>
    <label class="fld">显示名称</label>
    <input type="text" id="seName" maxlength="40" placeholder="给这个资源站起个名字"/>
    <label class="fld">订阅 / 接口地址（苹果CMS JSON）</label>
    <input type="text" id="seApi" placeholder="https://xxx.com/api.php/provide/vod/at/json"/>
    <div class="hint" id="seHint" style="margin-top:8px"></div>
    <div class="modal-foot">
      <button class="small" onclick="closeSrcEdit()">取消</button>
      <button class="primary small" onclick="saveSrcEdit()">保存</button>
    </div>
  </div>
</div>

<script>
const $=(id)=>document.getElementById(id);
const $$=(sel)=>Array.from(document.querySelectorAll(sel));
let CFG=null,KWS=[],STATUS=null,PRIO={};
const FEAT_DDUO=false,FEAT_ADULT=false;

function toast(m,kind){const t=$("toast");t.textContent=m;t.className="show "+(kind==="fail"?"fail":"ok");
  clearTimeout(window._tt);window._tt=setTimeout(()=>t.className="",2200);}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
async function jget(p){const r=await fetch(p);if(!r.ok)throw new Error(p+" "+r.status);return r.json();}
async function jpost(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(b||{})});return r.json();}

/* ---------- 页面切换 ---------- */
async function switchPage(page){
  $$(".page").forEach(el=>el.classList.toggle("active",el.id==="page-"+page));
  $$("[data-page]").forEach(el=>el.classList.toggle("active",el.dataset.page===page));
  window.scrollTo(0,0);
  if(page!=="overview")stopLivePoll();
  if(page==="overview"){renderOverview();startLivePoll();}
  if(page==="library"){
    Promise.all([loadLibs(),loadAccounts()]).then(()=>{
      renderLibrary();renderLibManage();renderAcl();
      if($("libPlacement")){
        $("libPlacement").value=LIBMETA.placement;
        $("anchorWrap").style.display=LIBMETA.placement==="after"?"":"none";
        if(LIBMETA.placement==="after")loadLocalLibs();
      }
    });
  }
  if(page==="sources"){try{await refreshSources();}catch(e){}try{loadHealth();}catch(e){}try{renderQuality();}catch(e){}}
  if(page==="data"){try{loadCacheStatus();}catch(e){}try{loadOnline();}catch(e){}}
  if(page==="network"){try{if(typeof initSubProxy==="function")initSubProxy();}catch(e){}try{initSrcProxy();}catch(e){}if($("updProxy"))$("updProxy").value=(CFG&&CFG.update_proxy)||"";}
  if(page==="playback"){try{loadAdfilter();}catch(e){}}
  if(page==="importsub"){try{if(!CFG)await loadStatus();}catch(e){}try{if(typeof loadDrpyMgmt==="function")loadDrpyMgmt();}catch(e){}}
  if(page==="about"){loadUpdate(false);}
  const more=$("moreNav"),moreBtn=$("moreNavBtn");
  if(more)more.classList.remove("show");
  if(moreBtn)moreBtn.classList.toggle("active",["importsub","pan","search","network","data","about"].includes(page));
}
function toggleMoreNav(){const m=$("moreNav");if(m)m.classList.toggle("show");}
$$("[data-page]").forEach(b=>b.addEventListener("click",()=>switchPage(b.dataset.page)));

/* ---------- 版本更新 ---------- */
let UPD_TIMER=null;
function $u(id){return document.getElementById(id);}
function updEdLabel(ed){return "统一版";}
async function loadUpdate(force){
  const rf=$u("updRefresh"),body=$u("updBody");
  if(force){
    if(rf){rf.disabled=true;rf.textContent="检查中…";}
    if(body)body.innerHTML='<span style="color:var(--ok,#16a34a)">正在向 GitHub 检查更新…</span>';
  }
  try{
    const r=await jget("/v/api/ext/update/check"+(force?"?force=1":""));
    renderUpdate(r);
    if(force){
      const u=r.update;
      if(r.status==="error")toast("检查失败："+(r.last_error||"无法访问 GitHub"),"fail");
      else if(u&&u.update_available)toast("发现新版本 v"+u.version);
      else toast("检查完成：已是最新版本"+(u&&u.version?" v"+u.version:""));
    }
  }catch(e){
    $u("updCur").textContent="—";
    body.innerHTML='<span style="color:var(--warn,#b45309)">检查更新失败：'+esc(e.message)+'（可能无法访问 GitHub）</span>';
    $u("updActions").style.display="flex";$u("updBtn").style.display="none";
    if(force)toast("检查更新失败："+e.message,"fail");
  }finally{
    if(rf){rf.disabled=false;rf.textContent="重新检查";}
  }
}
function renderUpdate(r){
  $u("updCur").textContent=r.current_version||"—";
  $u("updEdition").textContent=" · "+updEdLabel(r.edition);
  const u=r.update;
  const busy=["checking","downloading","installing"].includes(r.status);
  const body=$u("updBody"),actions=$u("updActions"),btn=$u("updBtn"),
        prog=$u("updProgress"),link=$u("updLink");
  if(link){if(u&&u.html_url){link.href=u.html_url;link.style.display="";}else{link.style.display="none";}}
  if(busy){
    body.innerHTML='<span style="color:var(--ok,#16a34a)">'+esc(r.progress||"处理中…")+'</span>';
    actions.style.display="flex";btn.style.display="none";
    prog.style.display="";prog.textContent="请稍候，安装完成后服务会自动重启，管理页稍后刷新即可。";
    return;
  }
  if(r.status==="done"){
    body.innerHTML='<span style="color:var(--ok,#16a34a)">✅ 已更新到 v'+esc(r.updated_version||"")+'，服务正在重启…请稍后刷新页面。</span>';
    actions.style.display="flex";btn.style.display="none";prog.style.display="none";return;
  }
  if(r.status==="error" && (!u || !u.update_available)){
    body.innerHTML='<span style="color:var(--warn,#b45309)">'+esc(r.last_error||"检查更新失败")+'</span>';
    actions.style.display="flex";btn.style.display="none";prog.style.display="none";return;
  }
  if(u && u.update_available){
    const edTip="（统一版）";
    const size=u.asset_size?` · ${(u.asset_size/1048576).toFixed(1)}MB`:"";
    body.innerHTML='<div><span style="color:var(--ok,#16a34a);font-weight:600">发现新版本 v'+esc(u.version)+edTip+'</span>'+size+'</div>'
      +'<div class="muted sm" style="white-space:pre-wrap;margin-top:6px;max-height:180px;overflow:auto">'+esc((u.notes||"").slice(0,1200))+'</div>';
    actions.style.display="flex";btn.style.display="";btn.disabled=false;btn.textContent="立即更新到 v"+u.version;
    prog.style.display="none";
  }else{
    const v=u&&u.version?("v"+u.version):"";
    const lc=r.last_check?(' · 上次检查 ' + new Date(r.last_check*1000).toLocaleString()):"";
    body.innerHTML='<span style="color:var(--muted,#666)">已是最新版本'+(v?"（GitHub 最新 "+v+"）":"")+'。</span>'
      +'<div class="hint sm" style="margin-top:4px">'+lc+'</div>';
    actions.style.display="flex";btn.style.display="none";prog.style.display="none";
  }
}
async function pollUpdate(){
  try{
    const r=await jget("/v/api/ext/update/status");
    renderUpdate(r);
    if(["downloading","installing"].includes(r.status)){
      UPD_TIMER=setTimeout(pollUpdate,1500);
    }else{UPD_TIMER=null;}
  }catch(e){}
}
document.addEventListener("DOMContentLoaded",()=>{
  const btn=document.getElementById("updBtn"),rf=document.getElementById("updRefresh");
  if(btn)btn.addEventListener("click",async()=>{
    if(!confirm("将从 GitHub 下载统一安装包（自动校验 sha256），并通过飞牛应用中心覆盖安装。\n\n安装期间本服务会短暂停止并自动重启（约十几秒），播放记录与已导入源会保留。\n\n确定立即更新？"))return;
    btn.disabled=true;btn.textContent="开始更新…";
    try{
      const rr=await fetch("/v/api/ext/update/now",{method:"POST",
        headers:{"Content-Type":"application/json","x-fnmedia-admin":"1"},
        body:JSON.stringify({confirm:true})});
      const r=await rr.json();
      if(r.code!==0){alert(r.msg||"更新启动失败");btn.disabled=false;loadUpdate(true);return;}
      pollUpdate();
    }catch(e){alert("更新启动失败："+e.message);btn.disabled=false;}
  });
  if(rf)rf.addEventListener("click",()=>loadUpdate(true));
  const ps=document.getElementById("updProxySave");
  if(ps)ps.addEventListener("click",async()=>{
    const v=(document.getElementById("updProxy").value||"").trim();
    if(v && !/^(https?|socks5h?):\/\//.test(v)){alert("代理地址需以 http:// 或 https:// 或 socks5:// 开头");return;}
    try{
      const r=await jpost("/v/api/ext/config",{update_proxy:v});
      if(r&&r.code===0){if(CFG)CFG.update_proxy=v;ps.textContent="已保存 ✓";setTimeout(()=>ps.textContent="保存代理",1500);loadUpdate(true);}
      else{alert("保存失败："+((r&&r.msg)||"未知错误"));}
    }catch(e){alert("保存失败："+e.message);}
  });
  // 回填已保存的更新代理
  (async()=>{try{
    const r=await jget("/v/api/ext/status");
    const inp=document.getElementById("updProxy");
    if(inp&&r.config&&r.config.update_proxy)inp.value=r.config.update_proxy;
  }catch(e){}})();
});

/* ---------- 概览 ---------- */
/* ---------- 本设备识别（自检） ---------- */
async function loadWhoami(){
  try{
    const r=await jget("/v/api/ext/whoami");const d=r.data||{};
    const kindMap={tv:"TV / 电视盒子（严格模型）",android:"Android 手机/平板",
      ios:"iPhone / iPad",web:"网页版",unknown:"未识别（按宽松处理）"};
    $("ovClientKind").textContent=d.label||"-";
    $("ovClient").textContent=(kindMap[d.kind]||d.kind)
      +(d.strict_card?" · 已启用严格字段规整":" · 宽松字段")
      +(d.is_mobile?" · 移动端":"");
    $("ovClientUa").textContent=d.ua||"";
  }catch(e){$("ovClient").textContent="识别失败："+e;}
}

/* ---------- 实时播放诊断 ---------- */
let LIVE_TIMER=null;
const LIVE_STAGE={library:"媒体库",browse:"片单",search:"搜索",detail:"详情",
  season:"季",episode:"选集",streams:"线路",play:"起播",media:"播放",
  hls:"清单",seg:"分片",direct:"直链",favorite:"收藏",history:"历史"};
const LIVE_ORIGIN={local:["local","var(--ok)"],mixed:["mixed","#2563eb"],
  upstream:["upstream","#b45309"],redirect:["redirect","#7c3aed"]};
function ago(a){return a<5?"刚刚":a<60?Math.floor(a)+"秒前":Math.floor(a/60)+"分前";}
function liveOrigin(o){const [t,col]=LIVE_ORIGIN[o]||[o,"var(--muted)"];
  return `<span class="tag" style="color:${col};background:transparent;border:1px solid ${col}33">${t}</span>`;}
async function loadLive(){
  if(!document.getElementById("liveDevices"))return;
  let d;
  try{ d=(await jget("/v/api/ext/live")).data||{devices:[]}; }
  catch(e){ $("liveState").textContent="连接失败"; $("livePulse").className="dot bad"; return; }
  $("liveState").textContent="实时"; $("livePulse").className="dot ok";
  LIVE_NOW=d.now||Date.now()/1000;
  const devs=d.devices||[];
  if(!devs.length){$("liveDevices").innerHTML='<div class="muted sm">暂无播放活动（在电视或手机上播一部片即可看到）</div>';return;}
  $("liveDevices").innerHTML=devs.map(dv=>{
    const kindColor={tv:"#b45309",android:"var(--ok)",ios:"#2563eb",web:"#7c3aed"}[dv.kind]||"var(--muted)";
    const sess=dv.session;
    const sessHtml=sess?`<div class="sm" style="margin-top:4px">
        ${sess.error?'<span class="tag amber">播放异常</span>':'<span class="tag">在播</span>'}
        <b>${esc(sess.title||"（未知名）")}</b>
        <span class="muted">· ${LIVE_STAGE[sess.stage]||sess.stage} · ${ago((d.now||dv.seen)-sess.at)}前</span>
        ${sess.error?`<div class="sm" style="color:var(--err);margin-top:2px">⚠ ${esc(sess.error)}</div>`:""}
      </div>`:"";
    const errBadge=dv.errors?` <span style="color:var(--err);font-weight:700">· ${dv.errors} 次错误</span>`:"";
    const rows=(dv.events||[]).slice(0,8).map(e=>{
      const st=e.status;const col=st>=400?"var(--err)":(st>=300?"#7c3aed":"var(--ok)");
      return `<div class="live-row">
        <span style="color:${col};font-weight:600;min-width:34px;display:inline-block">${st}</span>
        ${liveOrigin(e.origin)}
        <span class="tag" style="background:#eef1f5;color:var(--muted)">${LIVE_STAGE[e.stage]||e.stage}</span>
        ${e.title?`<span class="sm">${esc(e.title)}</span>`:""}
        <span class="muted sm" style="margin-left:auto;white-space:nowrap">${e.note?esc(e.note):""} ${ago((d.now||dv.seen)-e.t)}</span>
      </div>`;
    }).join("");
    return `<div class="live-dev">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="badge" style="background:${kindColor}18;color:${kindColor}">${esc(dv.label)}</span>
        <span class="muted sm">${esc(dv.ip)} · ${dv.reqs} 请求${errBadge} · 最近 ${ago(dv.ago)}</span>
      </div>
      ${sessHtml}
      <div class="live-rows">${rows}</div>
      <details class="muted sm"><summary style="cursor:pointer">UA</summary><span style="word-break:break-all">${esc(dv.ua)}</span></details>
    </div>`;
  }).join("");
  renderDiagnosis(devs);
}

/* ---------- 错误自动归因 ---------- */
let LIVE_DATA=[];let LIVE_NOW=0;
const STAGE_NAME={library:"媒体库",browse:"片单",search:"搜索",detail:"详情",
  season:"季",episode:"选集",streams:"线路",play:"起播",media:"播放",
  hls:"清单",seg:"分片",direct:"直链",favorite:"收藏",history:"历史"};
// 归因规则：按 阶段+状态+来源 推断根因与建议
function diagnoseDevice(dv){
  const errs=(dv.events||[]).filter(e=>e.err);
  if(!errs.length)return null;
  const byCode={};errs.forEach(e=>byCode[e.status]=(byCode[e.status]||0)+1);
  const first=errs[errs.length-1]; // 时间最早的错误
  const last=errs[0];
  let cause="",fix="",severity="warn";
  const st=last.status, stage=last.stage, origin=last.origin;
  if(st===403){
    if(["seg","hls","direct","media"].includes(stage)){cause="源站/CDN 鉴权拒绝（防盗链或链接过期）";fix="该资源站该线路受限，请切换其它线路或换源；属源站问题，非插件故障。";}
    else if(origin==="upstream"){cause="飞牛官方后端返回 403（权限/登录态）";fix="检查飞牛账号登录与媒体库访问权限。";}
    else{cause="请求被拒绝（403）";fix="重试或更换线路。";}
  }else if(st===404){
    if(["hls","seg","direct","media"].includes(stage)){cause="该线路解析不出可播地址（片源失效/无 HLS）";fix="播放源本身问题：请切换线路或换一个同名资源。";severity="src";}
    else if(stage==="detail"||stage==="browse"){cause="条目尚未解析出片源或已失效";fix="稍后重试，或用顶部搜索同名资源。";}
    else{cause="资源不存在（404）";fix="刷新或换源。";}
  }else if(st===502||st===504){
    cause=stage==="seg"?"分片上游抓取失败/超时":"插件向上游或资源站取数失败（502/504）";
    fix="资源站临时抖动，通常会自动重试；若反复出现请切换线路/源。";
  }else if(st===503){cause="暂无可播片源";fix="该条目暂未匹配到可播资源，稍后再试或搜索同名。";}
  else if(st===500){cause="服务端处理错误（500）";fix="请连同下方诊断信息反馈，多为某个异常响应头/数据形态，会持续完善兼容。";severity="err";}
  else if(origin==="upstream"&&st>=400){cause=`飞牛官方后端返回 ${st}`;fix="非插件注入路径，检查官方服务/网络。";}
  else{cause=`请求失败（HTTP ${st}，环节：${STAGE_NAME[stage]||stage}）`;fix="重试；如反复出现请复制诊断信息反馈。";}
  return {dv,cause,fix,severity,st,stage:STAGE_NAME[stage]||stage,
    at:last?ago(LIVE_NOW-last.t):"",count:errs.length,byCode};
}
function renderDiagnosis(devs){
  LIVE_DATA=devs;
  const box=$("liveDiagnosis");if(!box)return;
  const findings=devs.map(diagnoseDevice).filter(Boolean);
  if(!findings.length){box.innerHTML="";return;}
  const cls={warn:"amber",src:"",err:""};
  box.innerHTML=findings.slice(0,3).map(f=>{
    const color=f.severity==="err"?"var(--err)":(f.severity==="src"?"#2563eb":"#b45309");
    return `<div class="diag" style="border-left:3px solid ${color}">
      <div><b style="color:${color}">${esc(f.dv.label)}</b> · ${esc(f.stage)} · ${f.count} 次错误
        ${f.dv.session&&f.dv.session.title?(" · "+esc(f.dv.session.title)):""}</div>
      <div class="sm" style="margin-top:2px"><b>归因：</b>${esc(f.cause)}</div>
      <div class="sm muted" style="margin-top:2px"><b>建议：</b>${esc(f.fix)}</div>
    </div>`;
  }).join("");
}
async function copyDiagnosis(){
  try{
    const devs=LIVE_DATA;
    const now=new Date();
    const lines=[];
    lines.push("fnmedia-ext 播放诊断 · "+now.toLocaleString());
    lines.push("服务: "+location.host+" · 设备数 "+devs.length);
    devs.forEach((dv,i)=>{
      lines.push("");
      lines.push(`[${i+1}] ${dv.label} · ${dv.ip} · 请求${dv.reqs} 错误${dv.errors} · 最近${ago(dv.ago)}`);
      lines.push("UA: "+dv.ua);
      if(dv.session)lines.push("当前: "+(dv.session.title||"")+" · 环节"+(STAGE_NAME[dv.session.stage]||dv.session.stage)+(dv.session.error?(" · 异常 "+dv.session.error):" · 正常"));
      const f=diagnoseDevice(dv);
      if(f){lines.push("归因: "+f.cause);lines.push("建议: "+f.fix);}
      (dv.events||[]).slice(0,12).reverse().forEach(e=>{
        lines.push(`  ${e.status} [${e.origin}] ${STAGE_NAME[e.stage]||e.stage} ${e.p.replace(/[&<>]/g,"")} ${e.note||""}`);
      });
    });
    const text=lines.join("\n");
    try{await navigator.clipboard.writeText(text);}
    catch(_){const ta=document.createElement("textarea");ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand("copy");ta.remove();}
    toast("诊断信息已复制，可直接粘贴反馈");
  }catch(e){toast("复制失败："+e,"fail");}
}
function startLivePoll(){ if(LIVE_TIMER)return; LIVE_TIMER=setInterval(loadLive,3000); loadLive(); }
function stopLivePoll(){if(LIVE_TIMER){clearInterval(LIVE_TIMER);LIVE_TIMER=null;}}

function renderOverview(){
  if(!STATUS)return;
  const c=STATUS.charts&&STATUS.charts.counts||{};
  const hotN=(c.movie||0)+(c.tv||0)+(c.anime||0)+(c.variety||0);
  $("ovSrcN").textContent=CFG.sources.filter(s=>s.enabled).length;
  $("ovCacheN").textContent=STATUS.cached_titles||0;
  $("ovHotN").textContent=hotN;
  const cats=(STATUS.catalog&&STATUS.catalog.categories)||{};
  $("ovCatN").textContent=Object.values(cats).filter(x=>x.routes>0).length+"/4";
  $("ovHotMeta").textContent=(STATUS.charts&&STATUS.charts.refreshing?"正在刷新…":"最近刷新："+fmtTs(STATUS.charts&&STATUS.charts.updated_at))
    +` · 电影${c.movie||0}/剧${c.tv||0}/动漫${c.anime||0}/综艺${c.variety||0}`
    +(STATUS.charts&&STATUS.charts.last_error?(" · "+STATUS.charts.last_error):"");
}
/* ---------- 虚拟媒体库 ---------- */
let LIBS=[];
let LIBMETA={placement:"front",order:[],anchor:""};
let LOCALLIBS=[];
let ACCOUNTS=null;
async function loadLibs(){
  try{const r=await jget("/v/api/ext/libs");LIBS=(r.data&&r.data.libs)||[];
    LIBMETA={placement:r.data&&r.data.placement||"front",order:(r.data&&r.data.order)||[],anchor:(r.data&&r.data.anchor)||""};
    if($("catEnabled"))$("catEnabled").checked=!!(r.data&&r.data.catalog_enabled);
  }catch(e){LIBS=[];}
}
function catKeyOf(title){
  if(/电影/.test(title))return "movie"; if(/剧/.test(title))return "tv";
  if(/动漫|番/.test(title))return "anime"; if(/综艺/.test(title))return "variety"; return "";
}
function libCardHtml(L,ic){
  return `<div class="lib" data-g="${L.guid}"><div class="ic">${ic}</div>
    <div style="flex:1;min-width:0"><div class="t">${esc(L.title)}</div>
      <div class="c">${L.group==="hot"?(L.count+" 条"):(L.count?("路由 "+L.count):"建库中…")}</div></div>
    <label class="pin" title="在飞牛影视媒体库列表中置顶">
      <input type="checkbox" ${L.pinned?"checked":""} onchange="togglePin('${L.guid}',this)"/>置顶</label>
  </div>`;
}
async function togglePin(guid,el){
  try{
    const r=await jpost("/v/api/ext/lib/pin",{guid,pinned:el.checked});
    if(r.code===0){toast(el.checked?"已置顶":"已取消置顶");LIBS.forEach(x=>{if(x.guid===guid)x.pinned=el.checked;});}
    else{el.checked=!el.checked;toast(r.msg||"设置失败","fail");}
  }catch(e){el.checked=!el.checked;toast("设置失败","fail");}
}
function orderedManageLibs(){
  const rest=LIBS.filter(L=>!(LIBMETA.order||[]).includes(L.key));
  const head=(LIBMETA.order||[]).map(k=>LIBS.find(L=>L.key===k)).filter(Boolean);
  return head.concat(rest);
}
function renderLibManage(){
  const box=$("libManage"); if(!box)return;
  const arr=orderedManageLibs();
  const icon=L=>({chart:"🏆",catalog:"🌐",standalone:"📡","guard-native":"🛡",guard:"🛡",xlib:"☁️",t4:"🧩"}[L.kind]||"📁");
  box.innerHTML=arr.map((L,i)=>`
    <div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:8px">
      <span style="width:20px;text-align:center">${icon(L)}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(L.title)}</span>
      <button class="small" style="padding:2px 8px" ${i===0?"disabled":""} onclick="moveLib('${L.key}',-1)">↑</button>
      <button class="small" style="padding:2px 8px" ${i===arr.length-1?"disabled":""} onclick="moveLib('${L.key}',1)">↓</button>
      <label style="margin:0;font-size:12px;display:inline-flex;align-items:center;gap:4px" title="在飞牛影视中显示">
        <input type="checkbox" ${!L.hidden?"checked":""} onchange="toggleLibVisible('${L.key}',this)"/>显示</label>
    </div>`).join("");
}
async function moveLib(key,dir){
  const arr=orderedManageLibs(),keys=arr.map(L=>L.key);
  const i=keys.indexOf(key),j=i+dir;
  if(i<0||j<0||j>=keys.length)return;
  [keys[i],keys[j]]=[keys[j],keys[i]];
  LIBMETA.order=keys;
  const r=await jpost("/v/api/ext/lib/order",{order:keys,placement:LIBMETA.placement});
  if(r.code===0){await loadLibs();renderLibrary();toast("顺序已保存");}else toast(r.msg||"排序保存失败","fail");
}
async function toggleLibVisible(key,el){
  try{
    const r=await jpost("/v/api/ext/lib/visibility",{key,hidden:!el.checked});
    if(r.code===0){const L=LIBS.find(x=>x.key===key);if(L)L.hidden=!el.checked;
      toast(el.checked?"已设为显示":"已隐藏（所有账号不可见）");}
    else{el.checked=!el.checked;toast(r.msg||"设置失败","fail");}
  }catch(e){el.checked=!el.checked;toast("设置失败","fail");}
}
async function loadLocalLibs(){
  try{const r=await jget("/v/api/ext/local-libs");LOCALLIBS=(r.data||[]);
    const sel=$("libAnchor"); if(!sel)return;
    const cur=LIBMETA.anchor;
    sel.innerHTML=LOCALLIBS.map(L=>`<option value="${L.guid}" ${L.guid===cur?"selected":""}>${esc(L.title||L.guid.slice(0,8))}</option>`).join("");
    if(cur&&!LOCALLIBS.some(L=>L.guid===cur))sel.insertAdjacentHTML("afterbegin",`<option value="${cur}" selected>${cur.slice(0,10)}（可能已失效）</option>`);
  }catch(e){}
}
function onPlacementChange(){
  const p=$("libPlacement").value;
  $("anchorWrap").style.display=p==="after"?"":"none";
  if(p==="after"){loadLocalLibs().then(savePlacement);}else{savePlacement();}
}
async function savePlacement(){
  const p=$("libPlacement").value; LIBMETA.placement=p;
  const body={placement:p,order:LIBMETA.order};
  if(p==="after")body.anchor=($("libAnchor")||{}).value||LIBMETA.anchor||"";
  const r=await jpost("/v/api/ext/lib/order",body);
  if(r.code===0){LIBMETA.anchor=body.anchor||"";
    toast(p==="front"?"虚拟库将排在本地库之前":(p==="back"?"虚拟库将排在本地库之后":"已插到指定本地库之后"));}
  else toast(r.msg||"位置保存失败","fail");
}
async function loadAccounts(){
  try{ACCOUNTS=await jget("/v/api/ext/accounts");}catch(e){ACCOUNTS=null;}
  renderAcl();
}
function renderAcl(){
  const box=$("aclBox"); if(!box)return;
  if(!ACCOUNTS||!ACCOUNTS.data){box.innerHTML='<div class="hint">账号信息加载失败，请稍后刷新。</div>';return;}
  const d=ACCOUNTS.data;
  $("aclDefaultVisible").checked=!!d.default_visible;
  $("aclDefaultOnline").checked=!!d.default_online;
  const libs=d.libs||[];
  box.innerHTML=(d.users||[]).map(u=>{
    const acl=u.acl,configured=Array.isArray(acl);
    const allow=configured?new Set(acl):null;
    const checks=libs.map(L=>{
      const on=allow?allow.has(L.key):true,dis=allow?"":"disabled";
      return `<label style="display:inline-flex;align-items:center;gap:4px;margin:2px 10px 2px 0;font-size:12px">
        <input type="checkbox" data-k="${L.key}" ${on?"checked":""} ${dis}/>${esc(L.title)}</label>`;
    }).join("");
    const onlineOn=allow?allow.has("__online__"):true;
    return `<div style="border:1px solid var(--line);border-radius:9px;padding:9px;margin:8px 0">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;flex-wrap:wrap">
        <b>${esc(u.name)}${u.is_admin?' <span class="sub">（管理员始终全部可见）</span>':''}</b>
        <label style="margin-left:auto;font-size:12px"><input type="checkbox" ${configured?"checked":""} onchange="toggleAclMode('${u.guid}',this)"/> 单独授权</label>
      </div>
      <div data-acl-libs="${u.guid}" ${configured?"":"style='opacity:.5'"}>
        <label style="display:inline-flex;align-items:center;gap:4px;margin:2px 10px 2px 0;font-size:12px">
          <input type="checkbox" data-k="__online__" ${onlineOn?"checked":""} ${configured?"":"disabled"}/>🔍 在线搜索/片源</label>
        ${checks}
      </div>
      <div class="btn-row" style="margin-top:6px">
        <button class="primary small" onclick="saveAcl('${u.guid}')">保存该账号</button></div></div>`;
  }).join("") || '<div class="hint">未发现飞牛影视账号（在飞牛系统“用户管理”创建并用该账号登录影视后出现）。</div>';
}
function toggleAclMode(guid,el){
  const area=document.querySelector(`[data-acl-libs="${guid}"]`);
  area.querySelectorAll("input").forEach(cb=>{cb.disabled=!el.checked;});
  area.style.opacity=el.checked?"1":".5";
  if(!el.checked){saveAclRaw(guid,null);}
}
async function saveAcl(guid){
  const area=document.querySelector(`[data-acl-libs="${guid}"]`);
  const allow=Array.from(area.querySelectorAll("input:checked")).map(cb=>cb.getAttribute("data-k"));
  await saveAclRaw(guid,allow);
}
async function saveAclRaw(guid,allow){
  const r=await jpost("/v/api/ext/account/acl",{guid,allow});
  if(r.code===0){toast("账号授权已保存");await loadAccounts();}else toast(r.msg||"保存失败","fail");
}
async function saveAclDefaults(){
  const r=await jpost("/v/api/ext/account/acl",{
    default_visible:$("aclDefaultVisible").checked,
    default_online:$("aclDefaultOnline").checked});
  if(r.code===0){toast("默认策略已保存");await loadAccounts();}else toast(r.msg||"保存失败","fail");
}
function renderLibrary(){
  if(!STATUS)return;
  $("hotEnabled").checked=CFG.hot_library_enabled!==false;
  if($("catEnabled"))$("catEnabled").checked=CFG.catalog_library_enabled!==false;
  if($("t4Enabled"))$("t4Enabled").checked=CFG.t4_library_enabled!==false;
  const t4=LIBS.find(x=>x.kind==="t4");
  if($("t4Libs"))$("t4Libs").innerHTML=t4?`<div class="lib"><div class="ic">🧩</div><div><div class="t">${esc(t4.title)}</div><div class="c">${t4.count||0} 个插件</div></div></div>`:'<div class="hint">未配置 T4 源</div>';
  $("hotInterval").value=Math.round((CFG.hot_refresh_interval||21600)/3600);
  const c=STATUS.charts&&STATUS.charts.counts||{};
  const cats=(STATUS.catalog&&STATUS.catalog.categories)||{};
  const hotIc={movie:"🎬",tv:"📺",anime:"🌸",variety:"🎤"};
  if(LIBS.length){
    $("hotLibs").innerHTML=LIBS.filter(x=>x.group==="hot").map(L=>libCardHtml(L,hotIc[catKeyOf(L.title)]||"🔥")).join("");
    $("catLibs").innerHTML=LIBS.filter(x=>x.group==="catalog").map(L=>libCardHtml(L,"🌐")).join("");
  }else{
    $("hotLibs").innerHTML=[["movie","🎬","热门电影",c.movie||0],["tv","📺","热门剧集",c.tv||0],["anime","🌸","动漫新番",c.anime||0],["variety","🎤","热门综艺",c.variety||0]]
        .map(([k,ic,t,n])=>`<div class="lib"><div class="ic">${ic}</div><div><div class="t">${t}</div><div class="c">${n} 条</div></div></div>`).join("");
    $("catLibs").innerHTML=[["movie","🌐海量电影"],["tv","🌐海量剧集"],["anime","🌐海量动漫"],["variety","🌐海量综艺"]]
      .map(([k,t])=>{const n=(cats[k]||{}).routes||0;
        return `<div class="lib"><div class="ic">🌐</div><div><div class="t">${t}</div><div class="c">${n?("路由 "+n):"建库中…"}</div></div></div>`;}).join("");
  }
  $("hotStatus").textContent=(STATUS.charts&&STATUS.charts.refreshing?"正在后台刷新榜单…":"最近刷新："+fmtTs(STATUS.charts&&STATUS.charts.updated_at))
    +` · 数据源 ${(STATUS.charts&&STATUS.charts.provider_ok)||0}/${(STATUS.charts&&STATUS.charts.provider_total)||0}`
    +(STATUS.charts&&STATUS.charts.last_error?(" · "+STATUS.charts.last_error):"");
}
function fmtTs(ts){if(!ts)return"尚未刷新";const d=new Date(ts*1000),p=n=>String(n).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
async function saveHotCfg(){
  const body={hot_library_enabled:$("hotEnabled").checked,
    hot_refresh_interval:Math.max(1,Math.min(24,parseInt($("hotInterval").value)||6))*3600};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("热门设置已保存");await loadLibs();renderLibrary();}else toast(r.msg||"保存失败","fail");
}
async function saveCatCfg(){
  const body={catalog_library_enabled:$("catEnabled").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("海量四库设置已保存");await loadLibs();renderLibrary();}
  else toast(r.msg||"保存失败","fail");
}
async function saveT4Cfg(){
  const body={t4_library_enabled:$("t4Enabled").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("T4 选片设置已保存");await loadLibs();renderLibrary();}
  else toast(r.msg||"保存失败","fail");
}
async function refreshHot(){
  toast("开始刷新榜单…");
  const r=await jpost("/v/api/ext/hot/refresh",{});
  await loadStatus();await loadLibs();renderLibrary();
  toast(r.code===0?"榜单已刷新":(r.msg||"刷新失败"),r.code===0?"ok":"fail");
}

/* ---------- 片源质量 ---------- */
function renderQuality(){
  if(!STATUS)return;
  $("preferQ").checked=CFG.prefer_quality_source!==false;
  PRIO=STATUS.source_priority||{};
  const ov=CFG.source_priority||{};
  $("prioCount").textContent=Object.keys(ov).length+" 自定义";
  const rows=CFG.sources.filter(s=>!(s.runtime_unavailable||s.guard||s.spider_src
    ||(s.api||"").startsWith("guard://")||(s.api||"").startsWith("csprun://"))).map(s=>{
    const eff=PRIO[s.id]||0, custom=ov[s.id];
    const w=Math.max(4,Math.min(100,(eff+20)));
    return `<tr data-id="${s.id}">
      <td><div style="font-weight:500">${esc(s.name)} ${s.enabled?'':'<span class="muted sm">(停用)</span>'}</div>
        <div class="meta">${esc(s.api)}</div></td>
      <td><input type="number" class="prio-input" min="-100" max="100" data-prio
        value="${custom!=null?custom:''}" placeholder="${eff}"/></td>
      <td style="min-width:120px"><div class="prio-bar" style="width:${w}%"></div>
        <span class="muted sm">${custom!=null?('自定义 '+custom):'默认 '+eff}</span></td></tr>`;
  }).join("");
  $("prioRows").innerHTML=rows||'<tr><td colspan="3" class="muted">暂无源</td></tr>';
}
function collectPriority(){const m={};$$("[data-prio]").forEach(inp=>{
  const v=inp.value.trim();if(v!=="")m[inp.closest("tr").dataset.id]=parseInt(v)||0;});return m;}
async function savePriority(){
  const r=await jpost("/v/api/ext/source/priority",{priority:collectPriority()});
  if(r.code===0){toast("权重已保存");await loadStatus();renderQuality();}else toast(r.msg||"保存失败","fail");
}
async function resetPriority(){
  const r=await jpost("/v/api/ext/source/priority",{priority:{}});
  if(r.code===0){toast("已恢复默认权重");await loadStatus();renderQuality();}else toast("失败","fail");
}
async function savePreferQ(){
  const r=await jpost("/v/api/ext/config",{prefer_quality_source:$("preferQ").checked});
  if(r.code===0){CFG.prefer_quality_source=$("preferQ").checked;toast("已保存");}else toast("失败","fail");
}

/* ---------- 资源站 ---------- */
function renderSources(){
  const all=CFG.sources;
  const q=($("srcFilter")?.value||"").trim().toLowerCase();
  const srcs=q?all.filter(s=>(s.name||"").toLowerCase().includes(q)||(s.api||"").toLowerCase().includes(q)):all;
  $("srcCount").textContent=all.filter(x=>x.enabled).length+"/"+all.length+" 启用";
  const collapsed=$("srcCollapseBar")?.classList.contains("collapsed");
  const PREVIEW=12;
  let view=srcs,hiddenNote="";
  if(collapsed&&!q&&srcs.length>PREVIEW){
    view=srcs.slice(0,PREVIEW);
    hiddenNote=`<tr><td colspan="7" class="muted sm" style="text-align:center">还有 ${srcs.length-PREVIEW} 个未展开</td></tr>`;
  }
  const bar=$("srcCollapseBar");
  if(bar){bar.style.display=srcs.length>PREVIEW?"flex":"none";
    $("srcCollapseTxt").textContent=collapsed?`展开完整列表（${srcs.length} 个）`:"收起列表";}
  $("srcRows").innerHTML=view.map(src=>{
    let extraCell="",extraTag="";
    const androidDep=isAndroidSourceUi(src);
    if(androidDep)extraTag='<span class="tag off" title="主线已移除安卓运行时，该历史源已停用">安卓源不可用</span>'+extraTag;
    return `<tr data-id="${src.id}" class="${androidDep?"android-dep":""}">
      <td><input type="checkbox" class="en" ${src.enabled?"checked":""} ${androidDep?"disabled":""} style="accent-color:var(--accent);width:16px;height:16px"/></td>
      <td style="white:nowrap">
        <label title="把该资源站单独生成一张媒体库卡片" style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer">
          <input type="checkbox" class="sa" ${src.standalone?"checked":""}
           ${(src.enabled&&!(FEAT_DDUO&&src.xsrc))?"":"disabled"} style="width:15px;height:15px;accent-color:var(--accent)"/>库</label>
        <label title="非过滤：独立库内不按标题/备注过滤解说/预告/花絮" style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer;margin-left:4px">
          <input type="checkbox" class="lf" ${src.loose_filter?"checked":""}
           ${(src.enabled&&src.standalone&&!(FEAT_DDUO&&src.xsrc))?"":"disabled"} style="width:15px;height:15px;accent-color:var(--warn)"/>非过滤</label>
        ${extraCell}
      </td>
      <td><details class="src-detail">
        <summary>${esc(src.name)}${extraTag}${src.builtin?'':'<span class="tag amber">自定义</span>'}${src.standalone?'<span class="tag">媒体库</span>':''}</summary>
        <div class="src-detail-body">
          <div class="meta" title="${esc(src.api)}">${esc(src.api)}</div>
          <div class="hint sm" style="margin:4px 0 0">源 ID：${esc(src.id)} · ${androidDep?"已停用 · 安卓运行时已移除":(src.enabled?"已启用":"已停用")}${src.standalone?" · 独立媒体库":""}</div>
        </div>
      </details></td>
      <td class="st wait" data-st>${androidDep?"不可用":"未测"}</td>
      <td><button class="small" data-test ${androidDep?"disabled":""}>测试</button></td>
      <td><button class="small ghost" data-edit title="编辑名称/订阅链接">✎</button></td>
      <td><button class="del" data-del title="删除">✕</button></td>
    </tr>`;
  }).join("")+hiddenNote;
  $$("#srcRows tr[data-id]").forEach(tr=>{
    const id=tr.dataset.id;
    tr.querySelector(".en").addEventListener("change",()=>toggleEnable(id,tr.querySelector(".en").checked));
    const sa=tr.querySelector(".sa");sa.addEventListener("change",()=>toggleStandalone(id,sa.checked));
    const lf=tr.querySelector(".lf");if(lf)lf.addEventListener("change",()=>toggleLoose(id,lf.checked));
    tr.querySelector("[data-test]").addEventListener("click",()=>testOne(id,tr));
    tr.querySelector("[data-edit]").addEventListener("click",()=>openSrcEdit(id));
    tr.querySelector("[data-del]").addEventListener("click",()=>delSource(id,srcName(id)));
  });
}
async function refreshSources(){await loadStatus();renderSources();renderQuality();}
function toggleSrcCollapse(){const bar=$("srcCollapseBar");bar.classList.toggle("collapsed");renderSources();}
function toggleBox(id,bar){const el=$(id);if(!el)return;el.classList.toggle("collapsed");if(bar)bar.classList.toggle("collapsed");}
function openSrcEdit(id){
  const s=CFG.sources.find(x=>x.id===id);if(!s)return;
  $("seId").value=id;$("seName").value=s.name||"";$("seApi").value=s.api||"";
  const apiInp=$("seApi"),hint=$("seHint"),isGuard=!!s.guard;
  apiInp.disabled=isGuard||!!s.builtin;
  hint.textContent=isGuard?"该源仅可改显示名称。"
    :(s.builtin?"内置源仅可改显示名称；更换接口请删除后以自定义源重新添加。"
    :"自定义源可改名称与接口地址；改地址后源 ID 会重建，搜索缓存自动清空。");
  $("srcEditMask").classList.add("show");setTimeout(()=>$("seName").focus(),50);
}
function closeSrcEdit(){$("srcEditMask").classList.remove("show");}
document.addEventListener("keydown",e=>{if(e.key==="Escape"){const m=$("srcEditMask");if(m)m.classList.remove("show");}});
async function saveSrcEdit(){
  const id=$("seId").value,name=$("seName").value.trim(),api=$("seApi").value.trim();
  const s=CFG.sources.find(x=>x.id===id);if(!s)return;
  if(!name){toast("名称不能为空","fail");return;}
  if(!api){toast("接口地址不能为空","fail");return;}
  const body={id,name};if(!s.builtin&&!s.guard)body.api=api;
  const r=await jpost("/v/api/ext/source/update",body);
  if(r.code===0){toast("已保存");closeSrcEdit();await loadStatus();renderSources();}
  else toast(r.msg||"保存失败","fail");
}
function srcName(id){const s=CFG.sources.find(x=>x.id===id);return s?s.name:id;}
async function toggleEnable(id,on){
  const target=CFG.sources.find(s=>s.id===id);
  if(target&&(target.runtime_unavailable||target.guard||target.spider_src)){
    toast("安卓运行时已移除，该源不能启用","fail");renderSources();return;
  }
  const sources=CFG.sources.map(s=>({id:s.id,enabled:s.id===id?on:s.enabled}));
  const r=await jpost("/v/api/ext/config",{sources});
  if(r.code===0){CFG.sources.find(x=>x.id===id).enabled=on;
    $("srcCount").textContent=CFG.sources.filter(x=>x.enabled).length+"/"+CFG.sources.length+" 启用";toast(on?"已启用":"已停用");}
  else toast(r.msg||"保存失败","fail");
}
async function toggleStandalone(id,on){
  const r=await jpost("/v/api/ext/source/standalone",{id,standalone:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s){s.standalone=on;if(!on){s.loose_filter=false;s.adult=false;}}
    renderSources();toast(on?"已生成独立媒体库（后台建库中）":"已取消独立媒体库");}
  else{toast(r.msg||"操作失败","fail");renderSources();}
}
async function toggleLoose(id,on){
  const r=await jpost("/v/api/ext/source/loose-filter",{id,loose_filter:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s)s.loose_filter=on;renderSources();toast(on?"已开启非过滤":"已恢复严格过滤");}
  else{toast(r.msg||"操作失败","fail");renderSources();}
}
function setSt(tr,html,cls){const el=tr.querySelector("[data-st]");el.className="st "+cls;el.innerHTML=html;}
function isAndroidSourceUi(s){return !!(s&&(s.runtime_unavailable||s.guard||s.spider_src
  ||(s.api||"").startsWith("guard://")||(s.api||"").startsWith("csprun://")));}
async function testOne(id,tr){
  const src=CFG.sources.find(s=>s.id===id);
  if(isAndroidSourceUi(src)){setSt(tr,"不可用","bad");tr.querySelector("[data-test]").disabled=true;return;}
  setSt(tr,"测试中…","wait");tr.querySelector("[data-test]").disabled=true;
  try{const r=await jpost("/v/api/ext/source/test",{id});const d=r.data;
    if(d.ok)setSt(tr,`✓ ${d.elapsed_ms}ms · ${d.count}条${d.direct?(' · '+d.direct+'直链'):''}`,"ok");
    else setSt(tr,"✕ "+(d.error||"失败")+(d.elapsed_ms?(" · "+d.elapsed_ms+"ms"):""),"bad");
  }catch(e){setSt(tr,"✕ "+e,"bad");}
  tr.querySelector("[data-test]").disabled=isAndroidSourceUi(src);
}
async function testAll(){
  const bar=$("srcCollapseBar");if(bar&&bar.classList.contains("collapsed")){bar.classList.remove("collapsed");renderSources();}
  toast("正在按源类型测速…");
  $$("#srcRows [data-st]").forEach(el=>{el.className="st wait";el.textContent="等待分派…";});
  try{
    const r=await jpost("/v/api/ext/source/test-all",{});
    if(r.code!==0){toast(r.msg||"测速失败","fail");return;}
    (r.data||[]).forEach(d=>{const tr=$("srcRows").querySelector(`tr[data-id="${d.id}"]`);if(!tr)return;
      if(d.skipped)setSt(tr,esc(d.error||"请使用专用探活"),"wait");
      else if(d.ok)setSt(tr,`✓ ${d.elapsed_ms}ms · ${d.count}条${d.direct?(' · '+d.direct+'直链'):''}`,"ok");
      else setSt(tr,"✕ "+(d.error||"失败"),"bad");});
    toast("测速完成（扩展源已标注专用入口）");
  }catch(e){toast("测速失败："+e,"fail");}
}
let SRC_PROBE_TIMER=null;
async function sourceProbeStart(){
  try{
    await jpost("/v/api/ext/source/probe/start",{concurrency:4});
    $("srcProbeCancelBtn").style.display="";$("srcProbeProgress").style.display="";
    if(SRC_PROBE_TIMER)clearInterval(SRC_PROBE_TIMER);
    SRC_PROBE_TIMER=setInterval(sourceProbePoll,700);await sourceProbePoll();
  }catch(e){toast("启动探活失败："+e.message,"fail");}
}
async function sourceProbePoll(){
  try{
    const r=await jget("/v/api/ext/source/probe/status"),d=r.data||{};
    $("srcProbeProgress").textContent=`探活 ${d.done||0}/${d.total||0} · 可用 ${d.alive_count||0} · ${d.running?"进行中":"已结束"}`;
    (d.results||[]).forEach(x=>{
      const tr=$("srcRows").querySelector(`tr[data-id="${x.id}"]`);if(!tr)return;
      if(x.alive)setSt(tr,`✓ ${x.elapsed_ms||0}ms · ${x.count||0}条 · ${x.hit||"命中"}`,"ok");
      else setSt(tr,"✕ "+(x.error||"失效"),"bad");
    });
    if(!d.running){
      if(SRC_PROBE_TIMER){clearInterval(SRC_PROBE_TIMER);SRC_PROBE_TIMER=null;}
      $("srcProbeCancelBtn").style.display="none";
    }
  }catch(e){/* 单次轮询失败下轮再试，不清空已保留结果 */}
}
async function sourceProbeCancel(){
  await jpost("/v/api/ext/source/probe/cancel",{});
  if(SRC_PROBE_TIMER){clearInterval(SRC_PROBE_TIMER);SRC_PROBE_TIMER=null;}
  $("srcProbeCancelBtn").style.display="none";
  $("srcProbeProgress").textContent="探活已中止，已完成结果保留";
}
async function loadHealth(){
  const box=$("healthRows");if(!box)return;box.innerHTML='<tr><td colspan="6" class="muted">加载中…</td></tr>';
  try{
    const r=await (await fetch("/v/api/ext/source-health")).json();
    const rows=(r.data&&r.data.sources)||[];
    if(!rows.length){box.innerHTML='<tr><td colspan="6" class="muted">还没有数据，正常播放几次后会自动统计。</td></tr>';return;}
    const G=["健康","慢","高失败率","冷却中"];
    const GC=["ok","wait","bad","bad"];
    box.innerHTML=rows.map(s=>{
      const pct=Math.round((s.success_rate||0)*100);
      return `<tr><td>${esc(s.key)}</td><td>${s.samples}</td>
        <td>${pct}%</td><td>${(s.avg_latency||0).toFixed(1)}s</td>
        <td class="st ${GC[s.grade]||''}">${G[s.grade]||""}</td>
        <td><button class="small" data-health-key="${esc(s.key)}">清零</button></td></tr>`;
    }).join("");
    $$("#healthRows [data-health-key]").forEach(btn=>btn.addEventListener("click",()=>resetHealth(btn.dataset.healthKey)));
  }catch(e){box.innerHTML='<tr><td colspan="6" class="muted">读取失败</td></tr>';}
}
async function resetHealth(key){
  await jpost("/v/api/ext/source-health/reset",{key});loadHealth();
}
async function addSource(){
  const name=$("addName").value.trim(),api=$("addApi").value.trim(),standalone=$("addStandalone").checked;
  if(!api){toast("请填写接口地址","fail");return;}
  const r=await jpost("/v/api/ext/source/add",{name,api,standalone});
  if(r.code===0){toast("已添加");$("addName").value="";$("addApi").value="";$("addStandalone").checked=false;
    await refreshSources();const tr=$("srcRows").querySelector(`tr[data-id="${r.data.id}"]`);if(tr&&!standalone)testOne(r.data.id,tr);}
  else toast(r.msg||"添加失败","fail");
}
// ---------- 订阅抓取代理（仅拉订阅与 DRPY 脚本，不影响播放） ----------
function initSubProxy(){
  const p=(CFG&&CFG.subscription_proxy)||"";
  const en=$("subProxyEnable"),inp=$("subProxyUrl"),sb=$("subProxySaveBtn"),cb=$("subProxyClearBtn"),msg=$("subProxyMsg");
  if(!en)return;
  inp.value=p;en.checked=!!p;
  inp.disabled=!en.checked;sb.disabled=!en.checked;cb.disabled=!p;
  msg.textContent=p?"当前经代理拉取订阅资源":"当前直连（未设代理）";
  msg.style.color="";
}
function onSubProxyToggle(){
  const en=$("subProxyEnable"),inp=$("subProxyUrl"),sb=$("subProxySaveBtn");
  inp.disabled=!en.checked;sb.disabled=!en.checked;
  if(en.checked)inp.focus();
}
async function saveSubProxy(test){
  const inp=$("subProxyUrl"),msg=$("subProxyMsg");
  const proxy=inp.value.trim();
  if(!proxy){toast("请填写代理地址，或点清空直连","fail");return;}
  msg.textContent="保存并测试连通性…";msg.style.color="";
  try{
    const r=await jpost("/v/api/ext/subscription-proxy",{proxy});
    if(r.code!==0){msg.textContent="未保存："+(r.msg||"格式错误");msg.style.color="var(--danger)";return;}
    const d=r.data||{};
    if(CFG)CFG.subscription_proxy=d.proxy||proxy;
    if(test){
      msg.textContent=d.test_ok?("✓ 已保存，代理连通（"+(d.test_msg||"")+"），仅作用于订阅资源抓取")
                               :("已保存，但代理测试失败："+(d.test_msg||""));
      msg.style.color=d.test_ok?"var(--ok,#3a9)":"var(--warn,#d59a2b)";
    }else{msg.textContent="已保存";}
    $("subProxyClearBtn").disabled=false;
    toast("代理已保存");
  }catch(e){msg.textContent="请求失败："+String(e);msg.style.color="var(--danger)";}
}
async function clearSubProxy(){
  if(!confirm("清空订阅代理、改为直连？"))return;
  const r=await jpost("/v/api/ext/subscription-proxy",{proxy:""});
  if(r.code===0){if(CFG)CFG.subscription_proxy="";$("subProxyUrl").value="";$("subProxyEnable").checked=false;
    $("subProxyUrl").disabled=true;$("subProxySaveBtn").disabled=true;$("subProxyClearBtn").disabled=true;
    $("subProxyMsg").textContent="已清空，当前直连";$("subProxyMsg").style.color="";toast("已切回直连");}
  else toast(r.msg||"失败","fail");
}
async function loadAdfilter(){
  const box=$("adfStatus"), cb=$("adfEnable");
  try{
    const r=await jget("/v/api/ext/adfilter/status");
    if(r.code!==0){box.textContent="去广告状态读取失败";return;}
    const d=r.data||{};
    if(cb)cb.checked=!!d.enabled;
    const dyn=$("adfDynamic"); if(dyn)dyn.checked=d.dynamic!==false;
    let h="";
    if(d.enabled) h+='<span style="color:var(--ok,#3a9)">● 已启用</span>　';
    else h+='<span class="muted">○ 已关闭</span>　';
    h+="已收录订阅规则 <b>"+(d.rule_groups||0)+"</b> 组 / 域名 "+(d.hosts||[]).length
       +" 个 / 规则 "+(d.regex_count||0)+" 条";
    if((d.groups||[]).length){
      h+='<div class="muted sm" style="margin-top:6px">覆盖域名：'
        +d.groups.map(g=>esc((g.name||"")+"("+(g.hosts||[]).join(",")+")")).slice(0,12).join("、")
        +(d.groups.length>12?" 等":"")+'</div>';
    }else{
      h+='<div class="muted sm" style="margin-top:6px">尚无订阅自带规则；仍会通用清理标准广告标记。导入带 rules 的订阅后自动收录。</div>';
    }
    box.innerHTML=h;
  }catch(e){box.textContent="去广告状态读取失败："+esc(String(e));}
}
async function toggleAdfilter(on){
  try{
    const r=await jpost("/v/api/ext/adfilter/toggle",{enabled:!!on});
    if(r.code===0){toast(on?"已开启播放去广告":"已关闭播放去广告");loadAdfilter();}
    else{toast(r.msg||"操作失败","fail");}
  }catch(e){toast(String(e),"fail");}
}
async function toggleAdfilterDynamic(on){
  try{
    const r=await jpost("/v/api/ext/adfilter/toggle",{dynamic:!!on});
    if(r.code===0){toast(on?"已开启播放时动态检测":"已关闭动态检测");loadAdfilter();}
    else toast(r.msg||"操作失败","fail");
  }catch(e){toast(String(e),"fail");}
}
async function importSubscription(kind){
  const standalone=$("subStandalone").checked;
  const payload={standalone:standalone};
  payload.refresh_days=parseInt(($("subRefreshDays")?.value||"1"),10)||0;
  if(kind==="url"){payload.url=$("subUrl").value.trim();if(!payload.url){toast("请填写订阅 URL","fail");return;}}
  else{payload.text=$("subText").value.trim();if(!payload.text){toast("请粘贴配置 JSON","fail");return;}}
  const out=$("subResult");out.innerHTML='<div class="muted sm">正在解析订阅并探测源，请稍候…</div>';
  try{
    const r=await jpost("/v/api/ext/source/import-subscription",payload);
    if(r.code!==0){out.innerHTML='<div class="hint" style="color:var(--danger)">导入失败：'+esc(r.msg||"")+'</div>';return;}
    renderSubResult(r.data||{});
    toast("导入完成");
    try{loadAdfilter();}catch(e){}
    try{await refreshSources();}catch(e){}
    try{await loadDrpyMgmt();}catch(e){}
  }catch(e){out.innerHTML='<div class="hint" style="color:var(--danger)">导入出错：'+esc(String(e))+'</div>';}
}
function renderAndroidSubGroup(icon,title,rep){
  if(!rep)return "";
  const items=rep.items||[];
  if(!items.length&&!rep.error)return "";
  let h='<details class="subgrp android-dep"><summary class="subgrp-t" style="cursor:pointer">'
    +icon+' '+title+'　'+items.length+' 个 <span class="tag off">主线无安卓 · 不导入</span></summary>';
  if(rep.error)h+='<div class="hint" style="color:var(--danger)">'+esc(rep.error)+'</div>';
  else h+='<div class="muted sm" style="padding:4px 2px">这些源仅作识别报告，未写入源列表，不计入可用数量；CMS/T4/DRPY 仍按正常流程导入。</div>';
  items.slice(0,60).forEach(it=>{
    h+='<div class="subrow android-dep"><span>'+esc(it.name||it.key)+'</span><span class="tag off">未导入</span></div>';
  });
  if(items.length>60)h+='<div class="muted sm" style="padding:4px 2px">其余 '+(items.length-60)+' 个同类源同样未导入。</div>';
  return h+'</details>';
}
function renderSubResult(d){
  const cms=d.cms||{},skips=d.skipped_detail||[];
  let h="";
  // 汇总：新增 CMS/T4 与 DRPY 当前就绪分开计数，避免把旧更新项误称“新增”。
  const cmsOk=cms.added||0, t4=d.t4||{}, t4Ok=t4.added||0, dr=d.drpy||{};
  const drOk=(dr.items||[]).filter(x=>x.enabled).length;
  const androidN=((d.guard&&d.guard.items)||[]).length+((d.csp&&d.csp.items)||[]).length;
  h+='<div style="margin:8px 0;font-weight:600">共扫描 '+(d.scanned||0)+' 个站点'
    +(d.subscriptions?('（含 '+d.subscriptions+' 个子订阅）'):'')+'，CMS/T4 新增 <b style="color:var(--ok,#3a9)">'+(cmsOk+t4Ok)+'</b> 个，DRPY 当前就绪 <b style="color:var(--ok,#3a9)">'+drOk+'</b> 个。'
    +(androidN?'<span class="muted" style="font-weight:400">另有 '+androidN+' 个安卓依赖源未导入。</span>':'')+'</div>';
  // 苹果CMS
  if(cms.candidates||cms.added||cms.duplicate){
    h+='<div class="subgrp"><div class="subgrp-t">🍿 苹果CMS影视站　新增 '+cmsOk+' · 已存在 '+(cms.duplicate||0)+'</div>';
    (cms.added_names||[]).forEach(n=>{h+='<div class="subrow ok"><span>＋ '+esc(n)+'</span><span class="tag">已启用</span></div>';});
    h+='</div>';
  }
  // T4 选片源
  if(t4.candidates||t4.added||t4.duplicate){
    h+='<div class="subgrp"><div class="subgrp-t">🧩 T4 选片源　新增 '+t4Ok+' · 已存在 '+(t4.duplicate||0)+'</div>';
    (t4.added_names||[]).forEach(n=>{h+='<div class="subrow ok"><span>＋ '+esc(n)+'</span><span class="tag">已启用</span></div>';});
    h+='</div>';
  }
  // 远程 DRPY
  if(dr && dr.skipped_build){
    h+='<div class="hint" style="margin:10px 0">订阅中含远程 JS 爬虫，但当前为公开构建（无 Node 运行时），已跳过。</div>';
  } else if(dr && (dr.items||[]).length){
    h+='<div class="subgrp"><div class="subgrp-t">🧩 远程 JS 爬虫（DRPY）　就绪并启用 '+drOk+' / '+dr.items.length+'</div>';
    dr.items.forEach(it=>{
      const state=it.enabled?'<span class="tag">已启用</span>'
        :'<span class="tag warn">脚本未就绪</span>';
      h+='<div class="subrow"><span>'+esc(it.name||it.key)+'</span>'+state+'</div>';
    });
    h+='<div class="muted sm" style="padding:4px 2px">脚本若 401/下载失败，可在下方「已导入的远程 JS 爬虫」处重试。</div></div>';
  }
  if(dr && dr.error){h+='<div class="hint" style="color:var(--danger)">远程JS导入失败：'+esc(dr.error)+'</div>';}
  h+=renderAndroidSubGroup('🛡','Guard 加固源',d.guard);
  h+=renderAndroidSubGroup('🕷','普通 csp_ 爬虫',d.csp);
  // 跳过
  if(skips.length){
    h+='<details class="subgrp" style="margin-top:8px"><summary class="subgrp-t" style="cursor:pointer">⏭️ 跳过 '+ (d.skipped||skips.length) +' 个（网盘/直播/无法识别/探测失败）</summary>';
    const by={};skips.forEach(s=>{const r=s.reason||"其他";(by[r]=by[r]||[]).push(s.name);});
    Object.keys(by).forEach(r=>{h+='<div class="subrow muted"><span>'+esc(by[r].slice(0,10).join("、"))+(by[r].length>10?" 等":"")+'</span><span class="tag">'+esc(r)+'</span></div>';});
    h+='</details>';
  }
  $("subResult").innerHTML=h;
}
// ---------- 已导入普通爬虫源管理 ----------
async function delSource(id,name){
  if(!confirm("确定删除资源站「"+name+"」？"))return;
  const r=await jpost("/v/api/ext/source/delete",{id});
  if(r.code===0){toast("已删除");await refreshSources();}else toast(r.msg||"删除失败","fail");
}
async function restoreBuiltin(){
  const r=await jpost("/v/api/ext/source/restore",{});
  if(r.code===0){toast("已恢复内置源");await refreshSources();}else toast("失败","fail");
}

/* ---------- CMS 源导入 / 导出 ---------- */
async function exportCmsSources(){
  try{
    const r=await jget("/v/api/ext/source/export"),list=(r.data&&r.data.sources)||[];
    if(!list.length){toast("当前没有可导出的 CMS 源","fail");return;}
    const blob=new Blob([JSON.stringify(r.data,null,2)],{type:"application/json"});
    const a=document.createElement("a");a.href=URL.createObjectURL(blob);
    a.download="fnmedia-cms-sources.json";document.body.appendChild(a);a.click();
    setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},1000);
    toast("已导出 "+list.length+" 个 CMS 源");
  }catch(e){toast("导出失败："+e.message,"fail");}
}
async function importCmsFile(input){
  const f=input.files&&input.files[0];if(!f)return;
  try{
    const text=await f.text();let data;
    try{data=JSON.parse(text);}catch(_){toast("文件不是合法 JSON","fail");input.value="";return;}
    const body=Array.isArray(data)?{sources:data}:data;
    const r=await jpost("/v/api/ext/source/import-file",body);
    if(r.code===0){const d=r.data||{};
      toast(`导入完成：新增 ${d.added}，已存在跳过 ${d.skipped}，忽略非CMS ${d.ignored_noncms}`);
      await loadStatus();renderSources();
    }else toast(r.msg||"导入失败","fail");
  }catch(e){toast("导入失败："+e.message,"fail");}
  finally{input.value="";}
}

/* ---------- 搜索设置 / 噪音词 / 缓存 ---------- */
async function loadCacheStatus(){
  const box=$("cacheGrid");if(!box)return;
  try{
    const r=await jget("/v/api/ext/cache"),d=r.data||{};
    const fmtB=n=>n>=1048576?(n/1048576).toFixed(2)+" MB":(n>=1024?(n/1024).toFixed(1)+" KB":n+" B");
    const items=[
      ["titles","在线资源缓存",d.titles&&(fmtB(d.titles.bytes)+" · "+d.titles.entries+" 条"),"清后将重启服务"],
      ["charts","热门榜单缓存",d.charts&&fmtB(d.charts.bytes),"后台自动重抓"],
      ["catalog","海量库分类/分页",d.catalog&&(d.catalog.routes+" 路由 · "+d.catalog.pages+" 分页"),"下次浏览重建"],
      ["search","搜索结果内存缓存",d.search&&(d.search.entries+" 条"),"即时清空不重启"],
      ["health","源健康画像",d.health&&(d.health.entries+" 源"),"清空后重新画像"],
    ];
    box.innerHTML=items.map(([k,label,val,note])=>`<div class="cache-item">
      <div class="cn">${label}</div><div class="cv">${val||"-"}<br><span style="opacity:.7">${note}</span></div>
      <button class="small danger" style="width:100%" onclick="clearCaches(['${k}'])">清除</button></div>`).join("");
  }catch(e){box.innerHTML='<div class="muted sm">读取失败</div>';}
}
async function clearCaches(targets){
  const labels={titles:"在线资源缓存（会重启服务）",charts:"热门榜单",catalog:"海量库",search:"搜索缓存",health:"源健康画像"};
  const names=targets.map(t=>labels[t]||t).join("、");
  if(!confirm("确定清除："+names+" ？"))return;
  const r=await jpost("/v/api/ext/cache/clear",{targets});
  if(r.code===0){toast(r.msg||"已清除");
    if((r.data||{}).restart)setTimeout(()=>location.reload(),1800);
    else setTimeout(loadCacheStatus,600);
  }else toast(r.msg||"清除失败","fail");
}
function renderKw(){
  $("kwN").textContent="（共 "+KWS.length+" 个）";
  $("kwChips").innerHTML=KWS.map((k,i)=>`<span class="chip">${esc(k)}<b data-i="${i}" title="删除">×</b></span>`).join("");
  $("kwChips").querySelectorAll("b").forEach(b=>b.onclick=()=>{KWS.splice(+b.dataset.i,1);saveKw();});
}
async function saveKw(){
  const r=await jpost("/v/api/ext/junk-keywords",{keywords:KWS});
  if(r.code===0){KWS=r.data.junk_keywords;CFG.junk_keywords=KWS.slice();renderKw();toast("关键词已保存");}
  else toast(r.msg||"保存失败","fail");
}
async function addKw(){
  const v=$("kwAdd").value.trim();if(!v)return;
  v.split(/[,，、\s]+/).filter(Boolean).forEach(k=>{if(!KWS.includes(k))KWS.push(k);});
  $("kwAdd").value="";await saveKw();
}
function toggleKwEdit(){const box=$("kwEdit");const show=box.style.display==="none";
  box.style.display=show?"block":"none";if(show)$("kwArea").value=KWS.join("\n");}
async function applyKwText(){
  KWS=$("kwArea").value.split(/[,，、\n\r;；\s]+/).map(s=>s.trim()).filter(Boolean).filter((k,i,a)=>a.indexOf(k)===i);
  $("kwEdit").style.display="none";await saveKw();
}
async function resetKw(){
  if(!confirm("恢复为默认关键词列表？"))return;
  const r=await jpost("/v/api/ext/junk-keywords",{reset:true});
  if(r.code===0){KWS=r.data.junk_keywords;CFG.junk_keywords=KWS.slice();renderKw();$("kwEdit").style.display="none";toast("已恢复默认");}
  else toast("失败","fail");
}
async function saveCfg(){
  const body={max_per_keyword:parseInt($("maxn").value)||12,filter_junk:$("junk").checked,online_first:$("onlineFirst").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){toast("已保存");Object.assign(CFG,body);}else toast("保存失败 "+r.msg,"fail");
}
async function saveLines(){
  const body={line_fastest:$("lineFastest").checked,
    line_fastest_max:Math.max(2,Math.min(12,parseInt($("fastestMax").value)||6)),
    line_fastest_timeout:Math.max(1,Math.min(10,parseFloat($("fastestTimeout").value)||4)),
    line_autofail:$("lineAutofail").checked,
    direct_play_enabled:$("directPlay").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){toast("已保存");Object.assign(CFG,body);}else toast("保存失败 "+r.msg,"fail");
}
async function loadOnline(){
  const kw=$("kw").value.trim();
  const r=await jget("/v/api/ext/online?limit=500"+(kw?("&q="+encodeURIComponent(kw)):""));
  const list=r.data||[];$("cacheN").textContent=list.length;
  const tb=$("rows");
  if(!list.length){tb.innerHTML='<tr><td colspan="3" class="muted">暂无，先去飞牛搜索任意中文片名</td></tr>';return;}
  tb.innerHTML=list.map(x=>`<tr><td><div style="font-weight:500">${esc(x.title)}</div>
    <div class="meta">${esc(x.year||"")} ${esc(x.area||"")} · ${esc(x.remarks||"")}</div></td>
    <td>${esc(x.provider)}</td><td>${x.episodes}</td></tr>`).join("");
}

/* ---------- PanSou 盘搜（二级线路墙网盘高清来源） ---------- */
async function loadPansou(){
  const card=$("pansouCard");
  if(!card)return;
  try{
    const r=await jget("/v/api/ext/pansou/config");
    const d=r.data||{};
    if(d.supported===false){card.style.display="none";return;}
    card.style.display="";
    $("psEnabled").checked=!!d.enabled;
    $("psUrl").value=d.url||"http://127.0.0.1:8080";
    const types=(d.cloud_types||"quark,uc,aliyun").split(",").map(x=>x.trim()).filter(Boolean);
    $$(".ps-cloud").forEach(cb=>{cb.checked=types.includes(cb.value);});
    // 网盘登录态提示：三家全未登录时高亮，引导去下方扫码登录
    const login=d.pan_logged_in||{};
    const nameMap={quark:"夸克",uc:"UC",ali:"阿里云盘"};
    const stateTxt=Object.keys(nameMap).map(k=>`${nameMap[k]}:${login[k]?"已登录":"未登录"}`).join("　");
    const stateEl=$("psLoginState");if(stateEl)stateEl.textContent=stateTxt;
    const anyLogin=Object.values(login).some(Boolean);
    const warn=$("psLoginWarn");if(warn)warn.style.display=anyLogin?"none":"block";
  }catch(e){console.error("loadPansou",e);}
}
async function savePansou(){
  const msg=$("psMsg");
  try{
    const cloud=$$(".ps-cloud").filter(cb=>cb.checked).map(cb=>cb.value);
    if(!cloud.length){msg.textContent="至少选择一种可原画网盘";msg.style.color="var(--err)";return;}
    const r=await jpost("/v/api/ext/pansou/config",{
      enabled:$("psEnabled").checked,
      url:$("psUrl").value.trim(),
      cloud_types:cloud.join(",")
    });
    if(r.code===0){msg.textContent="已保存";msg.style.color="var(--ok)";toast("PanSou 配置已保存");}
    else{msg.textContent=r.msg||"保存失败";msg.style.color="var(--err)";}
  }catch(e){msg.textContent="请求失败："+e;msg.style.color="var(--err)";}
}
async function testPansou(){
  const msg=$("psMsg");
  try{
    msg.textContent="测试中…";msg.style.color="var(--muted,#6b7280)";
    const r=await jpost("/v/api/ext/pansou/test",{url:$("psUrl").value.trim()});
    const d=r.data||{};
    if(r.code===0&&d.ok){msg.textContent="连接成功（搜索 total="+d.total+"）";msg.style.color="var(--ok)";}
    else{msg.textContent=r.msg||"连接失败";msg.style.color="var(--err)";}
  }catch(e){msg.textContent="请求失败："+e;msg.style.color="var(--err)";}
}


/* ---------- 资源站取源代理（搜索/分类/测速，视频流仍直连） ---------- */
function initSrcProxy(){
  const p=(CFG&&CFG.source_proxy)||"";
  const en=$("srcProxyEnable"),inp=$("srcProxyUrl"),sb=$("srcProxySaveBtn"),cb=$("srcProxyClearBtn"),msg=$("srcProxyMsg");
  if(!en)return;
  inp.value=p;en.checked=!!p;inp.disabled=!en.checked;sb.disabled=!en.checked;cb.disabled=!p;
  msg.textContent=p?"取源请求按下方模式决定是否经代理，视频流仍直连":"当前未设代理（全部直连）";
  msg.style.color="";renderSrcProxyMode();
}
const SRC_MODE_HINT={
  auto:"🪄 智能：默认直连；只有直连失败的资源站自动切代理，其它站保持直连。",
  on:"始终走代理：所有资源站搜索/分类/测速/解析都经代理，视频流仍直连。",
  off:"全部直连：忽略已配置的代理地址。"
};
function renderSrcProxyMode(){
  const mode=(CFG&&CFG.source_proxy_mode)||"auto",seg=$("srcProxyMode"),hint=$("srcProxyModeHint");
  if(!seg)return;
  seg.querySelectorAll("button").forEach(b=>b.classList.toggle("active",b.dataset.mode===mode));
  if(hint)hint.textContent=SRC_MODE_HINT[mode]||SRC_MODE_HINT.auto;
}
async function setSrcProxyMode(mode){
  const proxy=$("srcProxyUrl").value.trim();
  if(mode!=="off"&&proxy&&proxy!==((CFG&&CFG.source_proxy)||""))await saveSrcProxy(false);
  const r=await jpost("/v/api/ext/source-proxy",{proxy:(CFG&&CFG.source_proxy)||"",mode});
  if(r.code===0){CFG.source_proxy_mode=(r.data&&r.data.mode)||mode;renderSrcProxyMode();
    toast({auto:"已切换为智能模式",on:"已切换为始终走代理",off:"已切换为全部直连"}[mode]);
  }else toast(r.msg||"切换失败","fail");
}
function onSrcProxyToggle(){
  const en=$("srcProxyEnable"),inp=$("srcProxyUrl"),sb=$("srcProxySaveBtn");
  inp.disabled=!en.checked;sb.disabled=!en.checked;if(en.checked)inp.focus();
}
async function saveSrcProxy(test){
  const inp=$("srcProxyUrl"),msg=$("srcProxyMsg"),proxy=inp.value.trim();
  if(!proxy){toast("请填写代理地址，或点清空直连","fail");return;}
  msg.textContent="保存并测试资源站连通性…";msg.style.color="";
  try{
    const mode=(CFG&&CFG.source_proxy_mode)||"auto";
    const r=await jpost("/v/api/ext/source-proxy",{proxy,mode});
    if(r.code!==0){msg.textContent="未保存："+(r.msg||"格式错误");msg.style.color="var(--err)";return;}
    CFG.source_proxy=proxy;CFG.source_proxy_mode=(r.data&&r.data.mode)||mode;renderSrcProxyMode();
    const d=r.data||{};
    if(test){msg.textContent=d.test_ok?("✓ 已保存，经代理测速成功（"+(d.test_msg||"")+"）")
      :("已保存，但经代理测速失败："+(d.test_msg||"可稍后在资源站页测速"));
      msg.style.color=d.test_ok?"var(--ok)":"var(--warn)";
    }else msg.textContent="已保存";
    $("srcProxyClearBtn").disabled=false;toast("源代理已保存");
  }catch(e){msg.textContent="请求失败："+String(e);msg.style.color="var(--err)";}
}
async function clearSrcProxy(){
  if(!confirm("清空资源站代理地址并全部直连？"))return;
  const r=await jpost("/v/api/ext/source-proxy",{proxy:"",mode:"off"});
  if(r.code===0){CFG.source_proxy="";CFG.source_proxy_mode="off";
    $("srcProxyUrl").value="";$("srcProxyEnable").checked=false;
    $("srcProxyUrl").disabled=true;$("srcProxySaveBtn").disabled=true;$("srcProxyClearBtn").disabled=true;
    renderSrcProxyMode();$("srcProxyMsg").textContent="已清空，当前直连";toast("已清空代理");
  }else toast(r.msg||"失败","fail");
}

/* ---------- 初始化 ---------- */
async function loadStatus(){
  STATUS=await jget("/v/api/ext/status");
  CFG=STATUS.config;
  PRIO=STATUS.source_priority||{};
}
async function init(){
  // 各初始化步骤独立容错：单个模块/元素缺失或接口异常只在控制台记录，
  // 不再让整个 init reject 导致管理页黑屏（重启窗口/精简构建尤其容易触发）。
  const safe=async(fn)=>{try{await fn();}catch(e){console.error(e);}};
  try{await loadStatus();}catch(e){console.error("loadStatus",e);}
  if(!CFG){
    const gs=$("globalState");if(gs){gs.textContent="状态接口未就绪，正在重试…";gs.className="dot bad";}
    setTimeout(()=>location.reload(),1500);return;
  }
  try{
    KWS=Array.isArray(CFG.junk_keywords)?CFG.junk_keywords.slice():[];
    $("maxn").value=CFG.max_per_keyword;
    $("junk").checked=CFG.filter_junk;
    $("onlineFirst").checked=CFG.online_first!==false;
    $("lineFastest").checked=CFG.line_fastest!==false;
    $("lineAutofail").checked=CFG.line_autofail!==false;
    if($("directPlay"))$("directPlay").checked=CFG.direct_play_enabled===true;
    $("fastestMax").value=CFG.line_fastest_max||6;
    $("fastestTimeout").value=CFG.line_fastest_timeout||4;
    renderKw();
  }catch(e){console.error("search form",e);}
  safe(renderSources);safe(initSrcProxy);safe(loadCacheStatus);
  try{
    const gs=$("globalState");gs.textContent="接管运行中 · 已缓存 "+STATUS.cached_titles;gs.className="dot ok";
    const ed=$("aboutEdition");if(ed)ed.textContent="统一无安卓版 · 在飞牛原生搜索与媒体库中注入在线影视";
  }catch(e){console.error("status bar",e);}
  safe(loadOnline);
  safe(renderOverview);
  safe(loadWhoami);
  safe(startLivePoll);
}
$("liveClear").addEventListener("click",async e=>{e.preventDefault();
  try{await fetch("/v/api/ext/live/clear",{method:"POST"});await loadLive();toast("已清空实时记录");}catch(_){}});
$("liveCopy").addEventListener("click",e=>{e.preventDefault();copyDiagnosis();});
$("kwAdd").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addKw();}});
$("kw").addEventListener("input",()=>{clearTimeout(window._t);window._t=setTimeout(loadOnline,250);});
init().catch(e=>{const gs=$("globalState");gs.textContent="未接管/接口不可用";gs.className="dot bad";console.error(e);});
</script>
</body>
</html>
"""
