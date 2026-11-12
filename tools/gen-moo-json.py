#!/usr/bin/env python3
"""按 Moo 应用源协议（https://github.com/Blue-Mink/moo）从 GitHub Release 生成
并维护仓库根的 moo.json。

设计：
- appname 固定 fnmedia-ext，必须与 FPK 包内 manifest 的 appname 一致；
- 每个发布 tag 写一条 releases.<version>，packages.all 指向该版【统一正式包】
  （GitHub Release 上的 fnmedia-ext-<ver>.fpk 裸名资产）；
- 顶层 version/download_url/sha256 始终等于【最新版】，老用户与 Moo 自动更新都能识别；
- changelog 取自该 tag 的 Release notes；size 用字节数写在 packages 内，
  顶层 size 用 MB（协议规定两处单位不同，切勿写反）；
- 幂等：读取已有 moo.json 合并进 releases（保留历史版本），只更新/新增当前版本。

用法：
  python3 tools/gen-moo-json.py --repo yujian0815/fnmedia-ext --tag v0.2.68 \
      [--asset fnmedia-ext-0.2.68.fpk] [--out moo.json]
需要 gh 已登录（读 release 资产 digest/size/notes）。
"""
import argparse
import json
import os
import subprocess
import sys

APPNAME = "fnmedia-ext"
DISPLAY_NAME = "飞牛影视在线扩展"
DESC = ("飞牛影视第三方在线资源扩展：零侵入可逆接管官方影视入口，聚合搜索、"
        "豆瓣热门榜单四库与资源站海量分类四库（电影/剧集/动漫/综艺），"
        "m3u8/mp4 代理播放、多线路切换、去广告、播放记录与智能续播，内置管理页。")
CATEGORIES = ["影音娱乐"]
HOMEPAGE = "https://github.com/yujian0815/fnmedia-ext"
ICON_PATH = "packaging/fpk/ICON.PNG"          # 公开仓库内图标（raw 直链）
MIN_FNOS = "1.2.0"                           # 与 manifest os_min_version 对齐
RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/"


def gh_json(*args: str) -> dict:
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def asset_key(name: str) -> str:
    """把资产名规整成可比较的键（去掉仓库前缀与扩展后缀差异）。"""
    return name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="yujian0815/fnmedia-ext")
    ap.add_argument("--tag", required=True, help="发布 tag，如 v0.2.68")
    ap.add_argument("--asset", default="",
                    help="统一正式包资产名；默认 fnmedia-ext-<ver>.fpk（裸名）")
    ap.add_argument("--out", default="moo.json")
    ap.add_argument("--updated-at", default="", help="覆盖该版发布时间(ISO)")
    args = ap.parse_args()

    version = args.tag[1:] if args.tag.startswith("v") else args.tag
    asset_name = args.asset or f"{APPNAME}-{version}.fpk"

    rel = gh_json("release", "view", args.tag, "--repo", args.repo,
                  "--json", "tagName,publishedAt,body,assets")
    assets = {a["name"]: a for a in rel.get("assets", [])}
    if asset_name not in assets:
        print(f"ERROR: release {args.tag} 找不到资产 {asset_name}，"
              f"现有：{sorted(assets)}", file=sys.stderr)
        return 1
    a = assets[asset_name]
    size_bytes = int(a.get("size") or 0)
    digest = a.get("digest") or ""
    sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    download_url = a.get("browser_download_url") or (
        f"https://github.com/{args.repo}/releases/download/{args.tag}/{asset_name}")
    updated_at = args.updated_at or rel.get("publishedAt") or ""
    notes = (rel.get("body") or "").strip()

    # 读已有索引，保留历史 releases
    data = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.setdefault("schema_version", "moo")
    data["source_info"] = {
        "name": "fnmedia-ext 官方源",
        "author": "yujian0815",
        "distributor": "yujian0815",
        "homepage": HOMEPAGE,
        "description": "飞牛影视在线扩展官方发布源",
    }
    apps = data.setdefault("apps", {})
    app = apps.setdefault(APPNAME, {})
    app.update({
        "display_name": DISPLAY_NAME,
        "app_type": "fpk",
        "platform": "all",
        "install_type": "package",
        "min_fnos": MIN_FNOS,
        "categories": CATEGORIES,
        "desc": DESC,
        "readme_url": "README.md",   # 相对 moo.json 目录补全，详情页「README」展示
        "homepage": HOMEPAGE,
        "bug_report_url": f"{HOMEPAGE}/issues",
        "icon_url": RAW_BASE.format(repo=args.repo) + ICON_PATH,
        "maintainer": "yujian0815",
        "author": "yujian0815",
    })

    releases = app.setdefault("releases", {})
    releases[version] = {
        "updated_at": updated_at,
        "changelog": notes,
        "packages": {
            "all": {
                "download_url": download_url,
                "sha256": sha,
                "size": size_bytes,          # 包级单位=字节
                "size_mb": round(size_bytes / 1048576, 2),
            }
        },
    }

    # 顶层字段始终指向最新版（协议：顶层优先；缺失才从 releases 推导）
    app["version"] = version
    app["download_url"] = download_url
    if sha:
        app["sha256"] = sha
    app["size"] = round(size_bytes / 1048576, 2)   # 条目级单位=MB
    app["updated_at"] = updated_at
    app["changelog"] = notes

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[moo] 已更新 {args.out}: {APPNAME} {version} "
          f"({size_bytes} bytes, sha256={'有' if sha else '无'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
