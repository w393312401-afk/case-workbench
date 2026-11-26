#!/usr/bin/env python3
"""速度体检：测正在运行的工作台，和保存的基线比较，变慢就报出来。

    python scripts/check_speed.py              比较；有变慢的项退出码为 1
    python scripts/check_speed.py --save       把现在的结果存成基线（只在确认当前速度正常时用）
    python scripts/check_speed.py --no-pages   只测接口，不开浏览器

测两样：各模块打开时读的接口（服务端耗时、传输体积、未变化是否免重传），以及在浏览器里逐个打开模块
（请求个数、分几轮发出、读完用时、主线程卡顿、再次进入的传输量）。只发读取请求，不改任何数据。
基线存在运行目录 service/speed-baseline.json，只属于这台电脑，不进备份。
"""
from __future__ import annotations

import argparse
import gzip
import http.client
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_config import keep_full_speed, load_config  # noqa: E402

RUNS = 7
# How much slower or larger than the baseline still passes. Data grows, so the limits leave room;
# when growth alone crosses them, look at the numbers and save a new baseline.
TIME_FACTOR, TIME_SLACK_MS = 1.6, 40
SIZE_FACTOR, SIZE_SLACK = 1.25, 2048
PAGE_TIME_FACTOR, PAGE_TIME_SLACK_MS = 1.6, 300
BLOCK_FACTOR, BLOCK_SLACK_MS = 1.6, 150
# Timer-driven reads and the model list a picker fetches when idle: they land in a page's window by chance,
# so they are not counted as that page's own.
POLLS = ("/api/activity", "/api/state?since", "/api/service/", "/api/ai/status", "/api/ai/tasks?projection=counts",
         "/api/ai/claude/login", "/api/data/status", "/api/variant-links/auto", "/api/ai/options")
PAGES = ("home", "library", "reviews", "directions", "content", "ai", "videos", "settings")


class Workbench:
    def __init__(self, port):
        self.port = port

    def get(self, path, tag=None):
        """One read: status, in-service total and CPU milliseconds, bytes on the wire, ETag, decoded body."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            headers = {"Accept-Encoding": "gzip"}
            if tag:
                headers["If-None-Match"] = tag
            started = time.perf_counter()
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            wall = (time.perf_counter() - started) * 1000
            timing = dict(part.strip().split(";dur=") for part in (response.getheader("Server-Timing") or "").split(",")
                          if ";dur=" in part)
            body = gzip.decompress(raw) if response.getheader("Content-Encoding") == "gzip" else raw
            return {"status": response.status, "total": float(timing["total"]) if "total" in timing else None,
                    "cpu": float(timing["cpu"]) if "cpu" in timing else None, "wall": wall, "bytes": len(raw),
                    "etag": response.getheader("ETag"), "body": body}
        finally:
            connection.close()

    def json(self, path):
        answer = self.get(path)
        if answer["status"] != 200:
            raise RuntimeError(f"{path} 返回 {answer['status']}")
        return json.loads(answer["body"])


def choose_scope(bench, product=None):
    """The product with the most scripts and its busiest direction: the heaviest everyday view."""
    if not product:
        counts = {}
        for script in bench.json("/api/creation").get("scripts", []):
            counts[script.get("product") or ""] = counts.get(script.get("product") or "", 0) + 1
        known = bench.json("/api/products").get("products", [])
        product = max(known, key=lambda name: counts.get(name, 0), default="") if known else ""
    directions = bench.json("/api/direction-catalog?" + urlencode({"product": product})).get("directions", [])
    busiest = max(directions, key=lambda row: ((row.get("performance") or {}).get("video_count") or 0,
                                                row.get("script_count") or 0), default=None)
    return product, (busiest or {}).get("direction_id")


def endpoints(product, direction):
    scoped = urlencode({"product": product})
    paths = {
        "案例总表": "/api/state",
        "产品": "/api/products",
        "首页·视频": "/api/performance/videos?" + urlencode({"projection": "home", "product": product}),
        "稿件": "/api/creation?" + scoped,
        "复盘记录": "/api/review-followups?" + scoped,
        "AI 任务": "/api/ai/tasks",
        "AI 任务数": "/api/ai/tasks?projection=counts",
        "AI 状态": "/api/ai/status",
        "任务动态": "/api/activity?limit=50",
        "内容列表": "/api/content?" + urlencode({"product": product, "view": "attention", "kind": "all", "status": "all",
                                             "q": "", "sort": "updated_desc"}),
        "内容视图": "/api/content/views?" + scoped,
        "需要你出手": "/api/needs-you?" + scoped,
        "图文总览": "/api/photo-overview?" + scoped,
        "制作单": "/api/production?" + scoped,
        "方向目录": "/api/direction-catalog?" + scoped,
        "方向列表": "/api/directions?" + urlencode({"archive": "active", "product": product}),
        "评论列表": "/api/reviews?" + urlencode({"product": product, "exclude_unlinked": 1, "sort": "useful", "limit": 50}),
        "评论汇总": "/api/reviews/summary",
        "销售汇总": "/api/performance/summary?shop=local",
        "销售视频": "/api/performance/videos?shop=local",
        "销售图表": "/api/performance/analytics?shop=local&daily=0",
        "达人": "/api/performance/creators",
        "活动": "/api/campaigns",
    }
    if direction:
        paths["方向进度"] = f"/api/direction-progress/{quote(direction)}?" + scoped
    return paths


def measure_endpoint(bench, path, runs=RUNS):
    first = bench.get(path)
    if first["status"] != 200:
        return {"error": f"返回 {first['status']}"}
    samples = [bench.get(path) for _ in range(runs)]
    result = {"total": statistics.median(row["total"] if row["total"] is not None else row["wall"] for row in samples),
              "cpu": statistics.median(row["cpu"] or 0 for row in samples), "bytes": samples[-1]["bytes"],
              "timed": samples[-1]["total"] is not None, "tagged": bool(samples[-1]["etag"])}
    if samples[-1]["etag"]:
        result["unchanged_free"] = bench.get(path, samples[-1]["etag"])["status"] == 304
    return result


PAGE_PROBE = """
async ([hash, polls]) => {
  performance.clearResourceTimings();
  const start = performance.now(); let blocked = 0, last = start;
  const observer = new PerformanceObserver(list => { for (const entry of list.getEntries()) blocked += entry.duration; });
  observer.observe({entryTypes: ['longtask']});
  location.hash = hash;
  let seen = 0, changed = performance.now();
  while (performance.now() - start < 20000) {
    await new Promise(resolve => setTimeout(resolve, 100));
    const count = performance.getEntriesByType('resource').length;
    if (count !== seen) { seen = count; changed = performance.now(); }
    else if (performance.now() - changed > 1500) break;
  }
  observer.disconnect();
  const reads = performance.getEntriesByType('resource')
    .filter(entry => entry.name.includes('/api/') && !polls.some(poll => entry.name.includes(poll)))
    .map(entry => ({path: entry.name.replace(location.origin, ''), start: entry.startTime - start,
                    end: entry.responseEnd - start, bytes: entry.transferSize}))
    .sort((a, b) => a.start - b.start);
  // A new round begins with a read that started only after an earlier round's read had answered.
  let rounds = 0, earliestEnd = -Infinity;
  for (const read of reads) {
    if (read.start >= earliestEnd) { rounds += 1; earliestEnd = read.end; }
    else earliestEnd = Math.min(earliestEnd, read.end);
  }
  return {requests: reads.length, rounds, done: Math.max(0, ...reads.map(read => read.end)), blocked,
          bytes: reads.reduce((sum, read) => sum + read.bytes, 0), paths: reads.map(read => read.path.split('?')[0])};
}
"""


def measure_pages(port, product):
    """Open every module in a fresh browser, then enter each once more: first visit and revisit figures."""
    from playwright.sync_api import sync_playwright
    results, problems = {}, []
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: problems.append(str(error)[:200]))
            started = time.perf_counter()
            page.goto(f"http://127.0.0.1:{port}/#/{quote(product)}/home", wait_until="load")
            page.wait_for_selector("#content *", timeout=30000)
            program = page.evaluate("performance.getEntriesByType('resource').filter(e => /\\.(js|css)(\\?|$)/.test(e.name)).length")
            results["整页打开"] = {"program_files": program, "ready": (time.perf_counter() - started) * 1000}
            page.wait_for_timeout(2500)
            for visit in ("首次", "再次"):
                for name in PAGES:
                    if visit == "首次" and name == "home":
                        continue  # the page was opened on it: that visit is the 整页打开 line
                    results[f"{name}·{visit}"] = page.evaluate(PAGE_PROBE, [f"#/{product}/{name}", list(POLLS)])
        finally:
            browser.close()
    return results, problems


def better_pages(first, second):
    """Per page, the better of two passes: a stray poll or a busy moment inflates one pass, never both."""
    merged = {}
    for name, row in first.items():
        other = second.get(name)
        if not other:
            merged[name] = row
        elif name == "整页打开":
            merged[name] = {key: min(value, other[key]) for key, value in row.items()}
        else:
            order = lambda item: (item["rounds"], item["requests"], item["done"])
            merged[name] = {**min(row, other, key=order), "blocked": min(row["blocked"], other["blocked"]),
                            "bytes": min(row["bytes"], other["bytes"])}
    return merged


def compare(now, base, label, key, factor, slack, unit, scale=1.0):
    if key not in base or key not in now:
        return None
    limit = base[key] * factor + slack
    if now[key] > limit:
        return f"{label}：{key_names.get(key, key)} {now[key] / scale:.0f}{unit}，基线 {base[key] / scale:.0f}{unit}（上限 {limit / scale:.0f}{unit}）"
    return None


key_names = {"total": "服务端耗时", "bytes": "传输体积", "done": "读完用时", "blocked": "主线程卡顿",
             "requests": "请求个数", "rounds": "请求轮数", "ready": "打开用时", "program_files": "程序文件数"}


def judge(now, base):
    """Every way the current run is worse than the baseline, as plain sentences."""
    findings = []
    for name, current in now["endpoints"].items():
        saved = base["endpoints"].get(name)
        if "error" in current:
            findings.append(f"{name}：{current['error']}")
            continue
        if not current.get("timed"):
            findings.append(f"{name}：回答里没有 Server-Timing（服务端计时被去掉了）")
        if current["bytes"] >= 1024 and not current.get("unchanged_free"):
            findings.append(f"{name}：内容没变也整份重传（ETag / 304 失效）")
        if not saved or "error" in saved:
            continue
        for found in (compare(current, saved, name, "total", TIME_FACTOR, TIME_SLACK_MS, " ms"),
                      compare(current, saved, name, "bytes", SIZE_FACTOR, SIZE_SLACK, " KB", 1024)):
            if found:
                findings.append(found)
    for name, current in (now.get("pages") or {}).items():
        saved = (base.get("pages") or {}).get(name)
        if not saved:
            continue
        if name == "整页打开":
            if current["program_files"] > saved["program_files"]:
                findings.append(f"整页打开：程序文件从 {saved['program_files']} 个变成 {current['program_files']} 个"
                                "（新加的脚本或样式没有并进合并包，见 program_assets.py）")
            found = compare(current, saved, name, "ready", PAGE_TIME_FACTOR, PAGE_TIME_SLACK_MS, " ms")
            if found:
                findings.append(found)
            continue
        if name.endswith("再次"):
            # Whether a second visit reads at all depends on how long ago the first one was (pages keep some
            # lists for 20-30 seconds); what must hold is that it transfers next to nothing and stays light.
            for found in (compare(current, saved, name, "blocked", BLOCK_FACTOR, BLOCK_SLACK_MS, " ms"),
                          compare(current, {"bytes": max(saved["bytes"], 4096)}, name, "bytes", SIZE_FACTOR, 4096, " KB", 1024)):
                if found:
                    findings.append(found)
            continue
        if current["rounds"] > saved["rounds"]:
            findings.append(f"{name}：请求从 {saved['rounds']} 轮变成 {current['rounds']} 轮（有读取改成了等上一个回来再发；"
                            f"公网上每多一轮约多半秒）")
        if current["requests"] > saved["requests"] + 1:
            extra = sorted(set(current["paths"]) - set(saved.get("paths", [])))
            findings.append(f"{name}：请求从 {saved['requests']} 个变成 {current['requests']} 个"
                            + (f"，新增 {'、'.join(extra)}" if extra else ""))
        for found in (compare(current, saved, name, "done", PAGE_TIME_FACTOR, PAGE_TIME_SLACK_MS, " ms"),
                      compare(current, saved, name, "blocked", BLOCK_FACTOR, BLOCK_SLACK_MS, " ms"),
                      compare(current, saved, name, "bytes", SIZE_FACTOR, 8192, " KB", 1024)):
            if found:
                findings.append(found)
    return findings


def page_findings(now, base):
    """Names of the pages the current run is worse on."""
    endpoint_names = set(now["endpoints"])
    return {found.split("：", 1)[0] for found in judge(now, base)} - endpoint_names


def overall(now, base):
    """One line on a slowdown shared by most reads: the engine itself, not one module's code."""
    ratios, working = [], []
    for name, current in now["endpoints"].items():
        saved = base["endpoints"].get(name) or {}
        if "total" in current and saved.get("total", 0) >= 20:
            ratios.append(current["total"] / saved["total"])
            working.append(current["cpu"] / current["total"] if current["total"] else 1)
    if len(ratios) < 5 or statistics.median(ratios) < 1.7:
        return None
    if statistics.median(working) >= 0.75:
        return (f"多数接口整体慢了约 {statistics.median(ratios):.1f} 倍，而且时间都花在计算上：先看引擎是否被系统降速"
                "（服务状态里的 full_speed）、机器是否正忙，再看具体模块。")
    return (f"多数接口整体慢了约 {statistics.median(ratios):.1f} 倍，时间主要花在等待上：有别的请求或后台任务占着"
            "（访客快照重建、正在跑的任务、别的页面在轮询），等空闲后再测一次。")


def run(bench, product, direction, pages):
    result = {"product": product, "direction": direction, "endpoints": {}}
    for name, path in endpoints(product, direction).items():
        result["endpoints"][name] = measure_endpoint(bench, path)
    if pages:
        result["pages"], result["page_errors"] = measure_pages(bench.port, product)
    return result


def retry_slow(bench, now, base):
    """A read measured while something else was busy gets a second chance; the better figure counts."""
    paths = endpoints(now["product"], now["direction"])
    for name, current in now["endpoints"].items():
        saved = base["endpoints"].get(name) or {}
        if "total" in current and "total" in saved and current["total"] > saved["total"] * TIME_FACTOR + TIME_SLACK_MS:
            again = measure_endpoint(bench, paths[name])
            if "total" in again and again["total"] < current["total"]:
                now["endpoints"][name] = again


def report(now, base=None):
    print(f"产品 {now['product'] or '全部'}" + (f" · 方向 {now['direction']}" if now["direction"] else ""))
    print(f"{'接口':<12}{'服务端':>8}{'其中计算':>9}{'体积':>9}" + (f"{'基线':>9}" if base else ""))
    for name, row in now["endpoints"].items():
        if "error" in row:
            print(f"{name:<12}{row['error']}")
            continue
        saved = ((base or {}).get("endpoints") or {}).get(name) or {}
        print(f"{name:<12}{row['total']:>6.0f}ms{row['cpu']:>7.0f}ms{row['bytes'] / 1024:>7.0f}KB"
              + (f"{saved['total']:>7.0f}ms" if "total" in saved else ""))
    if now.get("pages"):
        print(f"\n{'页面':<16}{'请求':>4}{'轮数':>5}{'读完':>9}{'卡顿':>8}{'传输':>8}")
        for name, row in now["pages"].items():
            if name == "整页打开":
                print(f"{name:<16}程序文件 {row['program_files']} 个，{row['ready']:.0f} ms 出现内容")
            else:
                print(f"{name:<16}{row['requests']:>4}{row['rounds']:>5}{row['done']:>7.0f}ms{row['blocked']:>6.0f}ms{row['bytes'] / 1024:>6.0f}KB")


def main(argv=None):
    parser = argparse.ArgumentParser(description="工作台速度体检")
    parser.add_argument("--save", action="store_true", help="把这次结果存成基线")
    parser.add_argument("--no-pages", action="store_true", help="只测接口，不开浏览器")
    parser.add_argument("--product", help="测哪个产品（默认稿件最多的那个，或基线里记的）")
    args = parser.parse_args(argv)
    keep_full_speed()  # a throttled measuring process would blame the workbench for its own slowness
    config = load_config(ROOT)
    bench = Workbench(config.get("port", 8767))
    baseline_path = Path(config["runtime_root"]) / "service" / "speed-baseline.json"
    base = None
    if baseline_path.is_file():
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
    try:
        status = bench.json("/api/service/status")
    except (OSError, RuntimeError, ValueError):
        print("工作台没有在运行，先启动再测。")
        return 2
    pages = not args.no_pages
    if pages:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            pages = False
            print("没有装 Playwright，这次只测接口。")
    if base and not args.product and not args.save:
        product, direction = base["product"], base["direction"]
    else:
        product, direction = choose_scope(bench, args.product)
    now = run(bench, product, direction, pages)
    if pages and (args.save or base is None):
        # A baseline is the best of two passes, so one unlucky moment does not loosen every later check.
        now["pages"] = better_pages(now["pages"], measure_pages(bench.port, product)[0])
    if base and not args.save:
        retry_slow(bench, now, base)
        if pages and any(name in (now.get("pages") or {}) for name in page_findings(now, base)):
            now["pages"] = better_pages(now["pages"], measure_pages(bench.port, product)[0])
    report(now, None if args.save else base)
    notes = []
    if status.get("full_speed") is False:
        notes.append("引擎没能退出系统的后台降速（full_speed 为否），所有接口都会慢几倍。")
    if status.get("code_stale") or status.get("task_host_code_stale"):
        notes.append("运行中的服务还是旧代码（改动后没有重启或引擎还没更新），测到的不是最新代码。")
    if now.get("page_errors"):
        notes.append("页面脚本报错：" + "；".join(now["page_errors"][:3]))
    if args.save or base is None:
        if base is None and not args.save:
            print("\n还没有基线。确认现在速度正常后运行：python scripts/check_speed.py --save")
            for note in notes:
                print("注意：" + note)
            return 0
        if notes:
            print("\n没有保存基线：")
            for note in notes:
                print("  " + note)
            return 1
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        now["saved_at"] = time.strftime("%Y-%m-%d %H:%M")
        now.pop("page_errors", None)
        baseline_path.write_text(json.dumps(now, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已存为基线：{baseline_path}")
        return 0
    findings = judge(now, base)
    summary = overall(now, base)
    print()
    if summary:
        print(summary)
    for note in notes:
        print("注意：" + note)
    if findings:
        print(f"比基线（{base.get('saved_at', '未记日期')}）变慢或变大的 {len(findings)} 项：")
        for found in findings:
            print("  " + found)
        print("确认是数据增长或有意的取舍后，才用 --save 更新基线。")
        return 1
    if notes:
        return 1
    print(f"速度与基线（{base.get('saved_at', '未记日期')}）一致。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
