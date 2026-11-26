"""Scrape new comments for one product from every platform through Apify (2026-11-09).

Each platform keeps the Apify actor its earlier exports came from, so a new file parses the
same way as the old ones (review_store.PARSERS). What to scrape is taken from the library itself:
the TikTok videos, Amazon ASINs, TikTok Shop products, Reddit searches and Facebook posts that
earlier comments came from. The page can edit these lists before starting.

The suggested start date for a platform is the newest comment the library already has from it,
so a scrape continues from where the last one stopped; the end date is today. Only comments
whose own date falls inside the chosen range are kept. The kept rows are written to
reviews/sources/<platform>/apify_<product>_<start>_<end>_<time>.json and the library syncs, so
everything downstream (topics, opportunities, skeletons) sees them like any other import.

The token is read from the user environment variable APIFY_TOKEN on every call. It only goes
into the Authorization header, never into a URL, a log or a reply.
"""
from __future__ import annotations

from contextlib import closing
import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import api_keys
import plugin_comments
from direction_store import product_key
from performance_store import product_models, product_short
from review_store import PARSERS, ReviewError
from review_taxonomy import PLATFORM_LABELS

KEY_NAME = "APIFY_TOKEN"
API = "https://api.apify.com/v2"
POLL_SECONDS = 10
DEFAULT_DAYS = 30  # a platform the library has nothing from yet
SALES_VIDEOS = 30  # videos suggested from the sales data when the library has no TikTok video source
# Actor that made each platform's earlier exports, the most results one run may bill, and the
# wording of one target on the page.
PLATFORMS = {
    "tiktok_video": {"actor": "clockworks/tiktok-comments-scraper", "limit": 3000, "target": "视频链接"},
    "amazon": {"actor": "junglee/amazon-reviews-scraper", "limit": 500, "target": "商品链接（含 ASIN）"},
    "tiktok_shop": {"actor": "vistics/tiktok-shop-product-reviews", "limit": 500, "target": "商品链接"},
    "reddit": {"actor": "trudax/reddit-scraper-lite", "limit": 300, "target": "搜索词"},
    "facebook": {"actor": "apify/facebook-comments-scraper", "limit": 500, "target": "帖子或短视频链接"},
}
DONE = {"SUCCEEDED"}
FAILED = {"FAILED", "ABORTED", "TIMED-OUT"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ScrapeError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_token():
    # Saved from 设置 → 密钥与令牌, or set by hand as a user environment variable.
    return api_keys.read(KEY_NAME)


def build_input(platform, targets, start, limit):
    """The actor input for one platform. Date filters are passed where the actor has one;
    every result is still filtered by its own date afterwards."""
    if platform == "tiktok_video":
        per_video = max(20, limit // max(1, len(targets)))
        return {"postURLs": targets, "commentsPerPost": per_video, "maxRepliesPerComment": 20}
    if platform == "amazon":
        return {"productUrls": [{"url": url} for url in targets], "sort": "recent", "reviewsCutoffDate": start,
                "filterByRatings": ["allStars"], "maxReviews": limit, "includeGdprSensitive": True}  # reviewer name and avatar
    if platform == "tiktok_shop":
        return {"startUrls": targets, "maxReviews": limit, "region": "US"}
    if platform == "reddit":
        return {"searches": targets, "searchComments": True, "searchPosts": False, "searchCommunities": False,
                "searchUsers": False, "sort": "new", "maxItems": limit, "commentDateLimit": start}
    if platform == "facebook":
        return {"startUrls": [{"url": url} for url in targets], "resultsLimit": limit, "includeNestedComments": True,
                "viewOption": "RECENT_ACTIVITY", "onlyCommentsNewerThan": start}
    raise ScrapeError(400, "不认识的平台。")


def flatten(platform, items):
    """TikTok Shop runs return one row per product with its reviews nested; the parser reads one row per review."""
    if platform != "tiktok_shop":
        return [item for item in items if isinstance(item, dict)]
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("reviews"), list):
            outer = {key: value for key, value in item.items() if key != "reviews"}
            rows.extend({**outer, **review} for review in item["reviews"] if isinstance(review, dict))
        else:
            rows.append(item)
    return rows


def keep_in_range(platform, items, product, start, end):
    """(kept rows, rows outside the range or without a date). Kept rows carry the product so the import files them under it."""
    kept, dropped = [], 0
    for item in flatten(platform, items):
        record = PARSERS[platform](item)
        posted = record and record.get("posted_on")
        if not posted or not start <= posted <= end:
            dropped += 1
            continue
        row = dict(item, productModel=product)
        if platform == "tiktok_video":
            row["product"] = product
        kept.append(row)
    return kept, dropped


class ApifyClient:
    def __init__(self, *, token_provider=read_token, transport=None, timeout=60):
        self.token_provider = token_provider
        self.transport = transport or self._urlopen
        self.timeout = timeout

    def token(self):
        value = self.token_provider()
        if not value:
            raise ScrapeError(503, "没有找到 Apify 令牌，请到「设置 → 密钥与令牌」填入。")
        return value

    def _urlopen(self, method, url, headers, body):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ScrapeError(502, f"连不上 Apify（{type(error).__name__}），请检查网络后再试。") from None

    def call(self, method, path, *, params=None, body=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        status, raw = self.transport(method, url, headers, data)
        try:
            payload = json.loads(raw or b"null")
        except ValueError:
            payload = None
        if status in (401, 403):
            raise ScrapeError(503, f"Apify 不认这个令牌（{status}），请重新复制令牌设到 {KEY_NAME}。")
        if status == 402:
            raise ScrapeError(402, "Apify 账户余额或额度不够，充值或升级后再试。")
        if status >= 400:
            message = (payload or {}).get("error", {}).get("message") if isinstance(payload, dict) else None
            raise ScrapeError(502, f"Apify 返回 {status}：{message or '请求失败'}")
        return payload

    def start(self, actor, actor_input, max_items):
        # maxItems caps what a pay-per-result actor may bill for this run.
        data = self.call("POST", f"/acts/{actor.replace('/', '~')}/runs", params={"maxItems": max_items}, body=actor_input)
        return (data or {}).get("data") or {}

    def run(self, run_id):
        return (self.call("GET", f"/actor-runs/{run_id}") or {}).get("data") or {}

    def me(self):
        return (self.call("GET", "/users/me") or {}).get("data") or {}

    def abort(self, run_id):
        return self.call("POST", f"/actor-runs/{run_id}/abort")

    def items(self, dataset_id):
        rows, offset = [], 0
        while True:
            page = self.call("GET", f"/datasets/{dataset_id}/items", params={"clean": "true", "format": "json", "offset": offset, "limit": 1000})
            page = page if isinstance(page, list) else []
            rows.extend(page)
            if len(page) < 1000:
                return rows
            offset += len(page)


class ReviewScraper:
    """One scrape job at a time; platforms in a job run side by side because the work happens at Apify."""

    def __init__(self, app, client=None, *, poll_seconds=POLL_SECONDS):
        self.app = app
        self.client = client or ApifyClient()
        self.poll_seconds = poll_seconds
        self.path = Path(app.reviews.root) / "apify-scrapes.json"
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.threads = []
        with self.lock:
            data = self._load()
            job = data.get("job")
            if job and job.get("status") == "running":
                # A run already started at Apify keeps going there: pick it up again instead of losing what was paid for.
                resumable = False
                for name, row in job["platforms"].items():
                    if row.get("status") == "running" and row.get("run_id"):
                        resumable = True
                    elif row.get("status") in {"queued", "running"}:
                        row.update(status="failed", error="工作台重启，这个平台还没开始抓，请再点一次。")
                self._save(data)
                if resumable:
                    self._launch(job)
                else:
                    self._finish()

    def busy(self):
        with self.lock:
            job = self._load().get("job") or {}
            return bool(job.get("status") == "running" or any(thread.is_alive() for thread in self.threads))

    # ---------- storage ----------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("targets", {})
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "targets": {}, "job": None}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _platform(self, name, **fields):
        with self.lock:
            data = self._load()
            data["job"]["platforms"][name].update(fields)
            self._save(data)

    # ---------- what the library already has ----------
    def _library(self, product):
        """Per platform: the newest comment date and the scrape targets earlier comments came from."""
        found = {name: {"latest": None, "targets": []} for name in PLATFORMS}
        database = self.app.reviews.database
        if not database.is_file():
            return found
        with self.app.reviews.lock, closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.create_function("product_key", 1, product_key, deterministic=True)
            rows = connection.execute(
                "SELECT platform, posted_on, source_ref, url FROM reviews WHERE product_key(product)=? AND source_file != ?",
                (product, plugin_comments.SOURCE_NAME)).fetchall()
        seen = {name: set() for name in PLATFORMS}
        for platform, posted, ref, url in rows:
            if platform not in found:
                continue
            if posted and (found[platform]["latest"] or "") < posted:
                found[platform]["latest"] = posted
            target = None
            if platform == "tiktok_video" and ref and url:
                target = url.split("?")[0]
            elif platform == "amazon" and ref:
                target = f"https://www.amazon.com/dp/{ref}"
            elif platform == "tiktok_shop" and ref:
                target = f"https://www.tiktok.com/view/product/{ref}"
            elif platform == "facebook" and ref:
                target = ref
            if target and target not in seen[platform]:
                seen[platform].add(target)
                found[platform]["targets"].append(target)
        # A platform the library has nothing from yet starts from what the sales data knows about the product.
        sales = None
        for name in ("tiktok_video", "tiktok_shop"):
            if not found[name]["targets"]:
                sales = self._from_sales(product) if sales is None else sales
                if sales[name]:
                    found[name].update(targets=sales[name], suggested=True)
        if not found["reddit"]["targets"]:
            found["reddit"].update(targets=[f"sharpal {product}"], suggested=not found["reddit"]["latest"])
        return found

    def _from_sales(self, product):
        """Scrape targets read from the sales data: the product's TikTok Shop pages and the videos
        linking it that drew the most comments (own and creator videos)."""
        found = {"tiktok_video": [], "tiktok_shop": []}
        database = getattr(getattr(self.app, "performance", None), "database", None)
        if not database or not Path(database).is_file():
            return found
        like = f"%{product}%"
        try:
            with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                videos = connection.execute("""
                    SELECT video_id, MAX(account), MAX(products), MAX(comments) FROM periods WHERE products LIKE ?1 GROUP BY video_id
                    UNION ALL SELECT video_id, MAX(account), MAX(products), MAX(comments) FROM creator_periods WHERE products LIKE ?1 GROUP BY video_id
                    UNION ALL SELECT video_id, MAX(account), MAX(products), SUM(comments) FROM daily WHERE products LIKE ?1 GROUP BY video_id""",
                    (like,)).fetchall()
                listed = connection.execute("SELECT DISTINCT product_id, product FROM photo_periods WHERE product LIKE ?1", (like,)).fetchall()
        except sqlite3.Error:
            return found
        shops, ranked = {}, {}
        for product_id, name in listed:
            if product_short(name) == product and re.fullmatch(r"\d{10,}", str(product_id or "")):
                shops[str(product_id)] = True
        for video_id, account, products, comments in videos:
            if product not in product_models(products):
                continue
            for name, product_id in re.findall(r"([^()]*)\((\d{10,})\)", products or ""):
                if product_short(name.strip(" ,")) == product:
                    shops[product_id] = True
            if video_id and account and (comments or 0) > 0:
                url = f"https://www.tiktok.com/@{str(account).lstrip('@')}/video/{video_id}"
                ranked[url] = max(ranked.get(url, 0), comments)
        found["tiktok_shop"] = [f"https://www.tiktok.com/view/product/{product_id}" for product_id in shops]
        found["tiktok_video"] = sorted(ranked, key=lambda url: -ranked[url])[:SALES_VIDEOS]
        return found

    def overview(self, product):
        product = product_key(product)
        if not product or product.lower() == "all":
            raise ScrapeError(400, "先选一个产品再抓评论。")
        library = self._library(product)
        with self.lock:
            data = self._load()
        saved = data["targets"].get(product, {})
        today = date.today().isoformat()
        platforms = []
        for name, spec in PLATFORMS.items():
            latest = library[name]["latest"]
            platforms.append({"platform": name, "label": PLATFORM_LABELS[name], "actor": spec["actor"], "limit": spec["limit"],
                              "target_label": spec["target"], "latest": latest,
                              "recommended_start": latest or (date.today() - timedelta(days=DEFAULT_DAYS)).isoformat(),
                              "targets": saved.get(name, library[name]["targets"]), "edited": name in saved,
                              "suggested": name not in saved and bool(library[name].get("suggested"))})
        starts = [row["recommended_start"] for row in platforms if row["targets"]]
        job = data.get("job")
        return {"product": product, "token": bool(self.client.token_provider()), "token_name": KEY_NAME, "today": today,
                "recommended_start": min(starts) if starts else (date.today() - timedelta(days=DEFAULT_DAYS)).isoformat(),
                "platforms": platforms, "job": job if job and job.get("product") == product else None,
                "busy": bool(job and job.get("status") == "running")}

    # ---------- running ----------
    def start(self, body):
        if not isinstance(body, dict):
            raise ScrapeError(400, "请求格式不对。")
        product = product_key(body.get("product"))
        if not product or product.lower() == "all":
            raise ScrapeError(400, "先选一个产品再抓评论。")
        start, end = str(body.get("start") or ""), str(body.get("end") or "")
        if not DATE.fullmatch(start) or not DATE.fullmatch(end) or start > end:
            raise ScrapeError(400, "日期要写成 年-月-日，开始不能晚于结束。")
        end = min(end, date.today().isoformat())
        chosen = body.get("platforms") if isinstance(body.get("platforms"), dict) else {}
        plan = {}
        for name, value in chosen.items():
            if name not in PLATFORMS or not isinstance(value, dict):
                continue
            targets = [str(row).strip() for row in (value.get("targets") or []) if str(row).strip()][:200]
            if targets:
                plan[name] = targets
        if not plan:
            raise ScrapeError(400, "至少选一个有抓取目标的平台。")
        self.client.token()  # fail now, not inside the background thread
        with self.lock:
            data = self._load()
            if data.get("job") and data["job"].get("status") == "running":
                raise ScrapeError(409, "上一次抓取还没结束，等它完成或先停止。")
            library = self._library(product)
            saved = data["targets"].setdefault(product, {})
            for name, targets in plan.items():
                if targets != library[name]["targets"]:
                    saved[name] = targets  # remember edits; untouched lists keep following the library
                else:
                    saved.pop(name, None)
            job = {"id": datetime.now().strftime("%Y%m%d-%H%M%S"), "product": product, "start": start, "end": end,
                   "status": "running", "started_at": _now(),
                   "platforms": {name: {"status": "queued", "targets": len(targets), "input": build_input(name, targets, start, PLATFORMS[name]["limit"])}
                                 for name, targets in plan.items()}}
            data["job"] = job
            self._save(data)
            self.cancel.clear()
            self._launch(job)
        return self.overview(product)

    def stop(self, body):
        product = product_key((body or {}).get("product"))
        self.cancel.set()
        with self.lock:
            job = self._load().get("job")
        for name, row in (job or {}).get("platforms", {}).items():
            if row.get("status") == "running" and row.get("run_id"):
                try:
                    self.client.abort(row["run_id"])
                except ScrapeError:
                    pass
        return self.overview(product or (job or {}).get("product"))

    def _launch(self, job):
        self.threads = [threading.Thread(target=self._run_platform, args=(job, name), daemon=True, name=f"apify-{name}")
                        for name, row in job["platforms"].items() if row.get("status") in {"queued", "running"}]
        for thread in self.threads:
            thread.start()
        threading.Thread(target=self._watch, args=(list(self.threads),), daemon=True, name="apify-finish").start()

    def _run_platform(self, job, name):
        row = job["platforms"][name]
        try:
            run_id, dataset_id = row.get("run_id"), row.get("dataset_id")
            if not run_id:
                if self.cancel.is_set():
                    raise ScrapeError(499, "已停止。")
                run = self.client.start(PLATFORMS[name]["actor"], row["input"], PLATFORMS[name]["limit"])
                run_id, dataset_id = run.get("id"), run.get("defaultDatasetId")
                if not run_id:
                    raise ScrapeError(502, "Apify 没有返回运行编号。")
                self._platform(name, status="running", run_id=run_id, dataset_id=dataset_id, started_at=_now())
            while True:
                run = self.client.run(run_id)
                status = run.get("status")
                dataset_id = run.get("defaultDatasetId") or dataset_id
                if status in DONE or status in FAILED:
                    break
                # stop() aborts the run at Apify; it then reports ABORTED and what it already scraped is still saved.
                time.sleep(self.poll_seconds)
            items = self.client.items(dataset_id) if dataset_id else []
            kept, dropped = keep_in_range(name, items, job["product"], job["start"], job["end"])
            saved = None
            if kept:
                folder = self.app.reviews.sources / name
                folder.mkdir(parents=True, exist_ok=True)
                saved = folder / f"apify_{job['product']}_{job['start']}_{job['end']}_{job['id']}.json"
                tmp = saved.with_suffix(".tmp")
                tmp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, saved)
            cost = run.get("usageTotalUsd")
            fields = {"fetched": len(items), "kept": len(kept), "outside": dropped, "file": saved.name if saved else None,
                      "finished_at": _now(), "cost_usd": round(cost, 3) if isinstance(cost, (int, float)) else None}
            if status in DONE:
                self._platform(name, status="done", **fields)
            else:
                self._platform(name, status="failed", error=f"Apify 运行{'已停止' if status == 'ABORTED' else '失败'}（{status}），已抓到的仍已保存。", **fields)
        except ScrapeError as error:
            self._platform(name, status="failed", error=str(error), finished_at=_now())
        except Exception as error:  # a scraper bug must not leave the job running forever
            self._platform(name, status="failed", error=f"抓取出错（{type(error).__name__}）。", finished_at=_now())

    def _watch(self, threads):
        for thread in threads:
            thread.join()
        self._finish()

    def _finish(self):
        with self.lock:
            data = self._load()
            job = data.get("job")
            if not job or job.get("status") != "running":
                return
            added = None
            if any(row.get("kept") for row in job["platforms"].values()):
                try:
                    result = self.app.reviews.sync()
                    added = result.get("added")
                except (ReviewError, OSError, sqlite3.Error) as error:
                    job["sync_error"] = f"评论库同步出错（{type(error).__name__}），点「重新同步」再试。"
            job.update(status="done", finished_at=_now(), added=added)
            self._save(data)
