/*
 * 独播库 https://www.dbku.tv —— drpy2 单文件源
 * ---------------------------------------------------------------
 * 站点：MacCMS + mytheme 模板，服务端直出 HTML，全部字段靠 HTML 解析。
 * 无可用 JSON 列表接口（/index.php/ajax/data、/api.php/provide/vod/ 均不可用）。
 *
 * drpy2 规则键  <->  用户侧函数名 映射
 *   init      -> host / headers / searchable / timeout（引擎初始化参数）
 *   home      -> class_parse（分类导航）
 *   homeVod   -> 推荐（首页 8 个板块的影片卡片）
 *   category  -> url + 一级（分类分页列表）
 *   detail    -> 二级（详情 + 播放列表）
 *   search    -> searchUrl + 搜索
 *   play      -> play_parse + lazy（player_data 解密 -> 直链 m3u8）
 *
 * 实测结论（详见 dbku-notes.md）：
 *   1. 分类分页真实地址是 /vodshow/{tid}--------{page}---.html；/vodtype/N-M.html 已 404。
 *   2. 搜索只支持 ?wd= 形式，且无有效分页（单次最多约 30 条）。
 *   3. 详情页只有一个播放源（#playlist1，无 #playlistN tab）。
 *   4. 播放页有 var player_data，encrypt=2 即 Base64 + 百分号编码，非 DRM。
 * ---------------------------------------------------------------
 */

var rule = {
    title: '独播库',
    host: 'https://www.dbku.tv',
    homeUrl: '/',
    // 分类分页：fypage 缺省为 1 -> /vodshow/2--------1---.html
    url: '/vodshow/fyclass--------fypage---.html',
    // 搜索必须是 query 形式；路径式 /vodsearch/{kw}----------1---.html 恒为 0 结果
    searchUrl: '/vodsearch/-------------.html?wd=**',
    searchable: 1,
    quickSearch: 0,
    filterable: 0,
    headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.dbku.tv/'
    },
    timeout: 15000,
    play_parse: true,
    limit: 6,

    // ===== home：分类 =====
    // 桌面导航只有 5 项，移动端下拉“频道”里才是全部 8 个分类，故取 .nav-list；
    // :gt(0) 跳过“首页”，正则从 /vodtype/13.html 里取 13 作为 fyclass。
    class_parse: '.dropdown-box .nav-list li:gt(0);a&&Text;a&&href;vodtype/(\\d+)\\.html',

    // ===== homeVod：首页 8 个板块（热剧推荐/陆剧/日韩剧/台泰剧/短剧/电影/综艺/动漫）=====
    // 首页 ul.myui-vodlist 共 8 个，卡片结构统一，一把梭即可
    推荐: 'ul.myui-vodlist li;.myui-vodlist__thumb&&title;.myui-vodlist__thumb&&data-original;.pic-text&&Text;.myui-vodlist__thumb&&href',

    // ===== category：分类列表（每页约 48 条，结构与首页卡片完全一致）=====
    一级: 'ul.myui-vodlist li;.myui-vodlist__thumb&&title;.myui-vodlist__thumb&&data-original;.pic-text&&Text;.myui-vodlist__thumb&&href',

    // ===== detail：详情 + 播放列表 =====
    二级: {
        // h1.title 是主标题；有的页面 title 里带副标题，故第二个兜底取 h4
        title: 'h1.title&&Text;h4.title&&Text',
        // 封面 img 用 data-original 懒加载真图
        img: '.myui-content__thumb img&&data-original;.myui-content__thumb a&&data-original',
        // 分类/地区/年份/更新/主演/导演都在 p.data 里；第 1 个 p.data 是最全的一行
        desc: '.myui-content__detail p.data:eq(0)&&Text;.myui-content__detail .data&&Text',
        // 简介：隐藏的 span.data 里是完整版（含 <p>），sketch 是截断版
        content: '#desc span.data&&Text;#desc .sketch.content&&Text',
        // 实测全部详情页只有 1 个播放源：页面里第 2 个 h3.title 就是“视频列表”标题
        // （第 1 个是“剧情简介”，第 3 个是“XX热播”侧栏），故用它当线路名
        tabs: 'h3.title:eq(1)',
        // 剧集锚点：<li><a class="btn btn-default" href="/vodplay/300-1-46.html">第46集</a></li>
        lists: '#playlist1 ul.myui-content__list li a'
    },

    // ===== search：搜索结果（.myui-vodlist__media 结构，与普通卡片不同）=====
    搜索: '#searchList li;.myui-vodlist__thumb&&title;.myui-vodlist__thumb&&data-original;.pic-text&&Text;.myui-vodlist__thumb&&href',

    // ===== play：播放页 player_data -> Base64 -> 百分号解码 -> m3u8 直链 =====
    lazy: String.raw`js:(function () {
        var UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
        var HOST = 'https://www.dbku.tv';
        var header = {'User-Agent': UA, 'Referer': HOST + '/'};
        var out = {parse: 0, url: '', header: header, headers: header};

        // 取播放页地址：drpy2 里 input 是注入的全局变量（本函数故意不声明形参，
        // 这样引擎“传参调用”和“注入全局变量”两种方式都能取到值）
        var purl = '';
        try {
            if (typeof input !== 'undefined' && input) {
                purl = (typeof input === 'string') ? input : (input.url || '');
            }
        } catch (e) { purl = ''; }
        if (!purl && arguments && arguments.length && arguments[0]) {
            purl = String(arguments[0]);
        }
        purl = String(purl || '');
        if (purl.indexOf('//') === 0) purl = 'https:' + purl;
        if (purl.charAt(0) === '/') purl = HOST + purl;
        if (!purl) return out;
        // 已经是直链（部分站点 detail 里直接给 m3u8/mp4）就不必再请求播放页
        if (/^https?:\/\//i.test(purl) && /\.(m3u8|mp4|flv)(\?|$)/i.test(purl)) {
            out.url = purl;
            return out;
        }

        var html = '';
        try {
            html = request(purl, {headers: header, timeout: 15000});
        } catch (e) {
            return out;
        }
        if (!html) return out;
        // 兼容 request 返回字符串 / {content:...} / {body:...} 三种形态
        if (typeof html !== 'string') {
            if (html && typeof html.content === 'string') html = html.content;
            else if (html && typeof html.body === 'string') html = html.body;
            else if (html && typeof html.data === 'string') html = html.data;
            else html = String(html);
        }

        // ---------- 1. 把 player_data 的 JSON 抠出来 ----------
        var js = '';
        var m = html.match(/player_data\s*=\s*(\{[\s\S]*?\})\s*;?\s*<\/script>/);
        if (m && m[1]) js = m[1];
        if (!js) {
            // 兜底：定位 player_data 后做括号配对扫描（避免正则被内层 } 截断）
            var p = html.indexOf('player_data');
            if (p > -1) {
                var s = html.indexOf('{', p);
                var depth = 0, quote = '', esc = false;
                for (var i = s; i > -1 && i < html.length; i++) {
                    var c = html.charAt(i);
                    if (esc) { esc = false; continue; }
                    if (c === '\\') { esc = true; continue; }
                    if (quote) { if (c === quote) quote = ''; continue; }
                    if (c === '"' || c === "'") { quote = c; continue; }
                    if (c === '{') depth++;
                    else if (c === '}') {
                        depth--;
                        if (depth === 0) { js = html.slice(s, i + 1); break; }
                    }
                }
            }
        }

        var pd = null;
        if (js) {
            try { pd = JSON.parse(js); } catch (e) { pd = null; }
        }

        // ---------- 2. Base64 解码（自带实现，不依赖 atob） ----------
        function b64decode(src) {
            var tbl = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
            var str = String(src).replace(/[^A-Za-z0-9\+\/\=]/g, '');
            var res = '', buf = 0, bits = 0;
            for (var i = 0; i < str.length; i++) {
                var ch = str.charAt(i);
                if (ch === '=') break;
                var idx = tbl.indexOf(ch);
                if (idx < 0) continue;
                buf = (buf << 6) | idx;
                bits += 6;
                if (bits >= 8) {
                    bits -= 8;
                    res += String.fromCharCode((buf >> bits) & 0xFF);
                }
            }
            return res;
        }

        // ---------- 3. 百分号解码（decodeURIComponent 失败时手写兜底） ----------
        function percentDecode(src) {
            try {
                return decodeURIComponent(src);
            } catch (e) {
                return String(src).replace(/%([0-9A-Fa-f]{2})/g, function (all, h) {
                    return String.fromCharCode(parseInt(h, 16));
                });
            }
        }

        var raw = (pd && pd.url) ? String(pd.url) : '';
        var real = '';

        if (raw) {
            var cand = raw;
            // encrypt=2：url 字段是 Base64（实测 5 例全部如此）；已是 http 串则直接百分号解码
            if (!/^https?%3A/i.test(cand) && !/^https?:/i.test(cand)) {
                var dec = b64decode(cand);
                if (dec && dec.length > 4) cand = dec;
            }
            real = percentDecode(cand);
        }

        // ---------- 4. 兜底：页面里直接出现的 m3u8 ----------
        if (!real || !/^https?:\/\//i.test(real)) {
            var mm = html.match(/https?:\\?\/\\?\/[^"'\s\\]+\.m3u8[^"'\s\\]*/);
            if (mm) real = mm[0].replace(/\\\//g, '/');
        }

        if (!real) return out;
        if (real.indexOf('//') === 0) real = 'https:' + real;
        out.url = real;
        // 兼容两种 drpy2 引擎约定：有的读返回值，有的读被覆盖后的全局 input
        try { input = out; } catch (e) { }
        return out;
    })()`
};
