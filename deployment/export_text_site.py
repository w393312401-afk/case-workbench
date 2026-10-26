"""Build a portable, read-only text snapshot from the existing local workbench.

Only the generated site/ folder is uploaded. This file and the original stores
stay local. All calls to the original service are GET requests.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_config import load_config
SUPPORT = ROOT / "deployment" / "text-only"
SITE = ROOT / "deployment" / "site"
LOCAL_PATH = re.compile(r"(?i)(?:(?<![A-Za-z0-9])[a-z]:[\\/]|file://|\\\\[^\\/\s]+[\\/])[^\s<>\"`\r\n]*")
MEDIA_MARKDOWN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
SECRET = re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{20,}|Bearer\s+[a-z0-9._-]{20,})\b")
PRIVATE_KEYS = {
    "paths", "path", "file_path", "source_file", "source_files", "source_paths",
    "runtime_root", "skill_root", "record_path", "reading_path", "thread_id",
    "session_id", "codex_thread_id", "codex_session_id", "bridge", "pid",
    "api_key", "api_token", "access_token", "oauth_token", "authorization",
    "cookie", "cookies", "password", "plan_token", "request_id", "local_path",
    "audio_file", "voiceover_path", "ledger_path", "output_path", "sources",
}


def clean(value, key=""):
    if isinstance(value, dict):
        result = {}
        if value.get("case_id") and isinstance(value.get("current"), dict):
            product = value.get("product") or ""
            title = value["current"].get("title") or value.get("title") or ""
            def models(text):
                return list(dict.fromkeys(re.findall(r"(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])", text.upper())))
            codes = models(product) or models(title)
            source = value["current"].get("source_case") or {}
            source_path = str(source.get("file_path") or "").replace("\\", "/")
            known = len(codes) == 1 and codes[0] == "129N" and re.search(
                r"/(?:tiktok-video-script-voiceover/examples|product-video-script-review/sources/video-cases)/own_products/129N_[^/]+\.md$", source_path, re.I)
            result["public_product_group"] = "SHARPAL " + codes[0] if len(codes) == 1 and (re.search(r"\bSHARPAL\b", product + " " + title, re.I) or known) else product
        for name, item in value.items():
            lowered = name.lower()
            if lowered == "sources":
                result[name] = []
                continue
            if lowered in PRIVATE_KEYS or lowered.endswith("_path"):
                continue
            if lowered == "token":
                result[name] = ""
            elif lowered == "assets":
                result[name] = {}
            elif lowered == "source_analysis":
                result[name] = clean(item)
            else:
                result[name] = clean(item, name)
        return result
    if isinstance(value, list):
        return [clean(item, key) for item in value]
    if isinstance(value, str):
        value = MEDIA_MARKDOWN.sub("[媒体已省略]", value)
        value = LOCAL_PATH.sub("[本机文件]", value)
        value = SECRET.sub("[凭据已省略]", value)
        # Personal contact addresses are not needed to browse workbench records.
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已省略]", value)
        return value
    return value


def strings(value):
    if isinstance(value, dict):
        for name, item in value.items():
            yield name
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def video_details(videos):
    """Read the existing facts once with the original deduplication rule.

    mode=ro prevents persistent SQLite writes. The original helper creates only
    temporary tables/views, which disappear when this connection closes.
    """
    sys.path.insert(0, str(ROOT))
    from performance_store import PerformanceStore
    config = load_config(ROOT)
    database = Path(config["runtime_root"]) / "performance" / "performance.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    series = {}
    try:
        PerformanceStore(config["runtime_root"])._facts(connection)
        for row in connection.execute("SELECT video_id, start, end, views, clicks, orders, items, gmv FROM facts ORDER BY video_id, start"):
            value = dict(row)
            identifier = value.pop("video_id")
            series.setdefault(identifier, []).append(value)
    finally:
        connection.close()
    judged = [v["window_views"] for v in videos if v["status"] in {"focus", "follow", "watch", "drop"}]
    gmvs = [v["gmv"] or 0 for v in videos]
    def top(values, value):
        return max(1, math.ceil(100 * sum(other >= value for other in values) / len(values)))
    details = {}
    for video in videos:
        detail = {**video, "judged_videos": len(judged), "all_videos": len(videos),
                  "gmv_top_pct": top(gmvs, video["gmv"] or 0), "series": series.get(video["video_id"], [])}
        if judged and video["status"] in {"focus", "follow", "watch", "drop"}:
            detail["window_top_pct"] = top(judged, video["window_views"])
        details[video["video_id"]] = detail
    return details


def export(base):
    def get(path):
        with urlopen(base.rstrip("/") + path, timeout=120) as response:
            return json.load(response)

    paths = {
        "state": "/api/state", "product_catalog": "/api/products", "review_summary": "/api/reviews/summary",
        "review_picks": "/api/reviews/picks", "review_edits": "/api/reviews/tag-edits",
        "performance_summary": "/api/performance/summary",
        "analytics": "/api/performance/analytics", "videos": "/api/performance/videos",
        "direction_catalog": "/api/direction-catalog", "directions": "/api/directions",
        "directions_all": "/api/directions?archive=all", "creation": "/api/creation",
        "materials": "/api/materials", "tasks": "/api/ai/tasks", "review_followups": "/api/review-followups",
    }
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = dict(zip(paths, executor.map(get, paths.values())))
    reviews, video_context, offset = [], {}, 0
    while True:
        result = get("/api/reviews?" + urlencode({"include_brand": 1, "include_irrelevant": 1,
                     "include_empty": 1, "limit": 200, "offset": offset}))
        reviews.extend(result["reviews"])
        video_context.update(result.get("videos", {}))
        offset += len(result["reviews"])
        if offset >= result["total"]:
            break
        if not result["reviews"]:
            raise RuntimeError("评论分页提前结束，拒绝导出不完整快照。")
    cases = responses["state"]["cases"]
    videos = responses["videos"]["videos"]
    tasks = responses["tasks"]["tasks"]
    scripts = responses["creation"]["scripts"]
    requests = [("cases", c["case_id"], "/api/cases/" + c["case_id"]) for c in cases]
    requests += [("tasks", t["task_id"], "/api/ai/tasks/" + t["task_id"]) for t in tasks]
    requests += [("scripts", s["id"], "/api/scripts/" + s["id"]) for s in scripts]
    for direction in responses["direction_catalog"]["directions"]:
        product, identifier = direction["product"], direction["direction_id"]
        requests.append(("direction_details", product + "|" + identifier,
                         "/api/direction-catalog/" + identifier + "?" + urlencode({"product": product})))
    full = {"cases": {}, "videos": video_details(videos), "tasks": {}, "scripts": {}, "direction_details": {}}
    # Cross-check representative statuses against the actual running API.
    checked = set()
    for video in videos:
        if video["status"] in checked:
            continue
        checked.add(video["status"])
        actual = get("/api/performance/videos/" + video["video_id"])["video"]
        expected = full["videos"][video["video_id"]]
        if any(expected.get(key) != value for key, value in actual.items()):
            raise RuntimeError("销售详情批量导出与工作台接口不一致，拒绝发布。")
    with ThreadPoolExecutor(max_workers=4) as executor:
        for (kind, identifier, _), result in zip(requests, executor.map(get, [r[2] for r in requests])):
            full[kind][identifier] = result.get("video") if kind == "videos" else result.get("task") if kind == "tasks" else result
    if (get("/api/state")["digest"] != responses["state"]["digest"] or get("/api/products") != responses["product_catalog"]
            or get("/api/creation")["revision"] != responses["creation"]["revision"]
            or get("/api/review-followups")["revision"] != responses["review_followups"]["revision"]):
        raise RuntimeError("导出期间本机内容有更新，请重新导出完整快照。")
    exported_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    bundle = clean({
        "meta": {"exported_at": exported_at, "mode": "read-only-text", "schema": 1},
        "state": responses["state"], "cases": full["cases"], "product_catalog": responses["product_catalog"],
        "reviews": {"summary": responses["review_summary"], "items": reviews,
                    "picks": responses["review_picks"]["picks"],
                    "tag_edits": responses["review_edits"]["edits"], "videos": video_context},
        "performance": {"summary": responses["performance_summary"], "analytics": responses["analytics"],
                        "videos": videos, "details": full["videos"]},
        "direction_catalog": responses["direction_catalog"], "directions": responses["directions"],
        "directions_all": responses["directions_all"], "direction_details": full["direction_details"],
        "creation": responses["creation"], "materials": responses["materials"], "scripts": full["scripts"], "review_followups": responses["review_followups"],
        "ai": {"status": {"available": False, "connected": False, "queued": 0, "running": 0,
                          "message": "文字浏览版，AI 创作请在本机使用。"},
               "options": {"models": [], "skills": [], "models_error": "文字浏览版"},
               "tasks": tasks, "details": full["tasks"]},
    })
    build_site(bundle)


def build_site(bundle, builder=None):
    """Finish a fresh build before replacing the previous upload directory."""
    parent = SITE.parent.resolve()
    if SITE.is_symlink() or (SITE.exists() and SITE.resolve().parent != parent):
        raise RuntimeError("部署目录不能指向其他本机目录。")
    temporary = Path(tempfile.mkdtemp(prefix=".text-site-build-", dir=parent))
    cleanup = True
    names = ("site", "text-snapshot.json", "export-report.json", "case-workbench-text.zip")
    saved, installed = [], []
    try:
        (builder or _build_site)(bundle, temporary / "site", temporary)
        try:
            for name in names:
                if (parent / name).exists():
                    os.replace(parent / name, temporary / ("previous-" + name))
                    saved.append(name)
            for name in names:
                os.replace(temporary / name, parent / name)
                installed.append(name)
        except BaseException:
            try:
                for name in reversed(installed):
                    os.replace(parent / name, temporary / name)
                for name in reversed(saved):
                    os.replace(temporary / ("previous-" + name), parent / name)
            except BaseException as rollback_error:
                cleanup = False
                raise RuntimeError("构建替换未完成，上一版备份保留在：" + str(temporary)) from rollback_error
            raise
    finally:
        # The freshly created, checked build directory contains only our build
        # and previous generated artifacts; never recurse through local stores.
        if cleanup and temporary.resolve().parent == parent and temporary.name.startswith(".text-site-build-"):
            shutil.rmtree(temporary)


def _build_site(bundle, site, output):
    exported_at = bundle["meta"]["exported_at"]
    # Expose a stable public digest, never the local service's write token.
    bundle["state"]["token"] = ""
    bundle["state"]["paths"] = {}
    bundle["state"]["public_mode"] = True
    bundle["state"]["exported_at"] = exported_at
    public_content = {key: value for key, value in bundle.items() if key != "meta"}
    public_content["state"] = {key: value for key, value in bundle["state"].items()
                               if key not in {"signature", "digest", "exported_at"}}
    revision = hashlib.sha256(json.dumps(public_content, ensure_ascii=False, sort_keys=True).encode())
    sources = [source for source in (ROOT / "static").iterdir() if source.suffix.lower() in {".html", ".js", ".css"}]
    sources += [SUPPORT / name for name in ("_worker.js", "public-mode.js", "public-mode.css")]
    for source in sorted(sources, key=lambda item: item.as_posix()):
        revision.update(source.relative_to(ROOT).as_posix().encode())
        revision.update(source.read_bytes())
    bundle["meta"]["content_revision"] = revision.hexdigest()
    digest_input = {**bundle, "state": {key: value for key, value in bundle["state"].items() if key not in {"signature", "digest"}}}
    digest = hashlib.sha256(json.dumps(digest_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    bundle["state"]["signature"] = bundle["state"]["digest"] = digest
    site.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "static").iterdir():
        if source.suffix.lower() in {".html", ".js", ".css"} and source.name not in {"activity.js", "activity.css"}:
            shutil.copy2(source, site / source.name)
    for name in ("_worker.js", "public-mode.js", "public-mode.css"):
        if not (SUPPORT / name).is_file():
            raise RuntimeError("部署支持文件尚未就绪：" + name)
        shutil.copy2(SUPPORT / name, site / name)
    app = (site / "app.js").read_text(encoding="utf-8")
    app = app.replace("function productFilterGroup(item) {", "function productFilterGroup(item) {\n  if (item.public_product_group) return item.public_product_group;", 1)
    (site / "app.js").write_text(app, encoding="utf-8")
    page = (site / "index.html").read_text(encoding="utf-8")
    page = page.replace('<link rel="stylesheet" href="/activity.css">', "").replace('<script src="/activity.js" defer></script>', "")
    # The public snapshot keeps the existing read-only creation records; scheduling stays local.
    page = page.replace('<link rel="stylesheet" href="/content-flow.css"><script src="/content-flow.js" defer></script>', "")
    page = page.replace('id="ai-nav" data-page="content"', 'id="ai-nav" data-page="ai"')
    page = page.replace('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', "")
    page = page.replace("<head>", '<head>\n  <meta name="public-exported-at" content="' + exported_at + '">\n  <script src="/public-mode.js"></script>', 1)
    page = page.replace("</head>", '  <link rel="stylesheet" href="/public-mode.css">\n</head>', 1)
    page = page.replace("<title>TK工作台</title>", "<title>TK工作台 · 文字浏览版</title>")
    (site / "index.html").write_text(page, encoding="utf-8")
    text = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    if any(LOCAL_PATH.search(item) or SECRET.search(item) for item in strings(bundle)):
        raise RuntimeError("部署数据仍含本机路径或凭据，拒绝打包。")
    # Keep the complete sanitized snapshot local. Cloudflare receives small,
    # lazy-loaded datasets, including per-case files and 32 video-detail shards.
    (output / "text-snapshot.json").write_text(text, encoding="utf-8")
    def save_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    product_rows = [*bundle["state"].get("cases", []), *bundle["performance"].get("videos", []),
                    *bundle["direction_catalog"].get("selling_points", []), *bundle["direction_catalog"].get("directions", []),
                    *bundle["creation"].get("scripts", []), *bundle["creation"].get("materials", [])]
    catalog = bundle.get("product_catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("products"), list):
        # Older saved bundles predate the authoritative product list. Only
        # those bundles need discovery; an explicitly empty catalog stays empty.
        catalog = {"products": sorted({value for value in [*(row.get("product") for row in product_rows),
                   *bundle["reviews"].get("summary", {}).get("products", [])]
                   if isinstance(value, str) and value.strip()}), "removed": [], "deleted": []}
    save_json(site / "public-data.json", {"meta": bundle["meta"], "state": bundle["state"],
              "products": catalog["products"], "product_catalog": catalog, "cases": {}, "split": True})
    save_json(site / "data" / "reviews.json", {"reviews": bundle["reviews"]})
    save_json(site / "data" / "performance.json", {"performance": {key: bundle["performance"][key] for key in ("summary", "analytics", "videos")}})
    shards = [{} for _ in range(32)]
    for identifier, detail in bundle["performance"]["details"].items():
        shards[int(identifier[-5:]) % 32][identifier] = detail
    for number, details in enumerate(shards):
        save_json(site / "data" / f"video-shard-{number}.json", {"performance": {"details": details}})
    for identifier, case in bundle["cases"].items():
        save_json(site / "data" / f"case-{identifier}.json", {"cases": {identifier: case}})
    save_json(site / "data" / "ai.json", {"ai": bundle["ai"]})
    save_json(site / "data" / "creation.json", {**{key: bundle[key] for key in ("creation", "materials", "scripts")}, "review_followups": bundle.get("review_followups", {"entries": [], "revision": "public"})})
    save_json(site / "data" / "direction-catalog.json", {key: bundle[key] for key in ("direction_catalog", "direction_details")})
    save_json(site / "data" / "directions.json", {key: bundle[key] for key in ("directions", "directions_all")})
    (site / "_routes.json").write_text(json.dumps({"version": 1, "include": ["/*"], "exclude": []}), encoding="utf-8")
    (site / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n  X-Robots-Tag: noindex, nofollow\n", encoding="utf-8")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    allowed = {file.name for file in (ROOT / "static").iterdir() if file.suffix.lower() in {".html", ".js", ".css"}}
    allowed.update({"_worker.js", "public-mode.js", "public-mode.css", "public-data.json", "_routes.json", "_headers", "robots.txt"})
    allowed.update({"data/reviews.json", "data/performance.json", "data/ai.json", "data/creation.json", "data/direction-catalog.json", "data/directions.json"})
    allowed.update(f"data/video-shard-{number}.json" for number in range(32))
    allowed.update(f"data/case-{identifier}.json" for identifier in bundle["cases"])
    files = [file for file in site.rglob("*") if file.is_file()]
    for file in files:
        if file.relative_to(site).as_posix() not in allowed:
            raise RuntimeError("部署目录有未列入本次构建的文件，拒绝上传：" + str(file.relative_to(site)))
        if file.stat().st_size > 25 * 1024 * 1024:
            raise RuntimeError("文件超过 Cloudflare 25 MiB 上传限制：" + file.name)
        if file.suffix.lower() not in {".html", ".js", ".css", ".json", ".txt", ""}:
            raise RuntimeError("部署目录含非文字文件：" + file.name)
    target = output / "case-workbench-text.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.relative_to(site).as_posix())
    report = {"exported_at": exported_at, "cases": len(bundle["cases"]), "reviews": len(bundle["reviews"]["items"]),
              "videos": len(bundle["performance"]["videos"]), "tasks": len(bundle["ai"]["tasks"]), "scripts": len(bundle["scripts"]),
              "directions": len(bundle["direction_details"]), "json_bytes": len(text.encode()),
              "zip_bytes": target.stat().st_size, "file_count": len(files)}
    (output / "export-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


# fly retired the public text site on 2026-10-15: it had no login and carried sales, GMV, scripts
# and AI history. While RETIRED is True every export publishes only a notice page, no data.
RETIRED = True
RETIRED_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>已停用</title><style>body{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f6f4;color:#222}main{max-width:420px;padding:24px;text-align:center}</style></head><body><main><h1>此页面已停用</h1><p>内容已下线。</p></main></body></html>"""
RETIRED_WORKER = """const PAGE = %s;
export default { async fetch() { return new Response(PAGE, { status: 410, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex, nofollow" } }); } };
"""


def _build_retired(bundle, site, output):
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(RETIRED_PAGE, encoding="utf-8")
    (site / "_worker.js").write_text(RETIRED_WORKER % json.dumps(RETIRED_PAGE, ensure_ascii=False), encoding="utf-8")
    (site / "_headers").write_text("/*\n  X-Robots-Tag: noindex, nofollow\n  Cache-Control: no-store\n", encoding="utf-8")
    (output / "text-snapshot.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    (output / "export-report.json").write_text(json.dumps({"retired": True}), encoding="utf-8")
    with zipfile.ZipFile(output / "case-workbench-text.zip", "w") as archive:
        for path in sorted(site.iterdir()):
            archive.write(path, path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8767")
    parser.add_argument("--package-only", action="store_true")
    args = parser.parse_args()
    if RETIRED:
        build_site({"meta": {"retired": True}, "state": {}}, builder=_build_retired)
    elif args.package_only:
        build_site(json.loads((ROOT / "deployment" / "text-snapshot.json").read_text(encoding="utf-8")))
    else:
        export(args.base)
