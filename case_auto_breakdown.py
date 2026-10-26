"""收为案例后的后台任务：取得原视频 → 关联到案例 → 调用拆解。

Each case has at most one job, kept as JSON under runtime/evidence/auto-breakdown/<case_id>.json.
The video source is, in order: a finished file already bound to a video publication with the same
video ID, the yt-dlp Python module, a yt-dlp executable on PATH, then reading TikTok's public page.
The breakdown step is a replaceable callable (`analyzer`); until the case-analysis skill is wired in,
the default analyzer only reports that the step is waiting, so nothing is guessed or written.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import urllib.request
from uuid import uuid4

from case_insights import CaseTags, comments, contrasts, funnel, transcript_gate
from managed_video import MAX_VIDEO_BYTES, VIDEO_EXTENSIONS

CASE_ID = re.compile(r"PVR-[0-9a-f]{12}")
VIDEO_ID = re.compile(r"\d{6,25}")
ACTIVE = {"queued", "downloading", "attaching", "analyzing"}
STEP_LABELS = {"queued": "排队中", "downloading": "正在取得原视频", "attaching": "正在关联视频到案例",
               "analyzing": "正在拆解视频", "completed": "已完成", "analyzer_pending": "视频已关联，拆解待接入",
               "failed": "未完成", "interrupted": "工作台重启后中断"}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
DOWNLOAD_TIMEOUT = 600
# Downloads are network-bound and run wider than analyses, which each start a model run on the PC.
PARALLEL_DOWNLOADS = 4
PARALLEL_ANALYSES = 3
BATCH_LIMIT = 10


class DownloadError(RuntimeError):
    pass


class AnalyzerPending(RuntimeError):
    """The breakdown step has not been connected yet; the job stops without a failure."""


def case_revision(case):
    # Same digest as server.revision(); kept here so this module never imports server.
    return hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pending_analyzer(app, case_id, video_path, job):
    raise AnalyzerPending("视频已关联到案例；自动拆解这一步还在接入视频案例拆解 skill，接好后点「重新拆解」即可。")


def _finish_file(folder, video_id):
    files = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
    if not files:
        return None
    best = max(files, key=lambda path: path.stat().st_size)
    size = best.stat().st_size
    if not 0 < size <= MAX_VIDEO_BYTES:
        raise DownloadError("下载到的视频为空或超过 2GB。")
    target = folder / f"{video_id}{best.suffix.lower()}"
    if best != target:
        best.rename(target)
    return target


def _with_yt_dlp_module(url, folder, video_id):
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return None, "未安装 yt-dlp（Python 模块）"
    options = {"outtmpl": str(folder / f"{video_id}.%(ext)s"), "format": "best[ext=mp4]/best",
               "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
               "max_filesize": MAX_VIDEO_BYTES, "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(options) as client:
            client.download([url])
    except Exception as exc:  # yt-dlp raises its own DownloadError types
        return None, f"yt-dlp 下载失败：{str(exc)[:200]}"
    return _finish_file(folder, video_id), ""


def _with_yt_dlp_exe(url, folder, video_id):
    exe = shutil.which("yt-dlp")
    if not exe:
        return None, "本机 PATH 中没有 yt-dlp"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run([exe, "--no-playlist", "-f", "best[ext=mp4]/best", "-o", str(folder / f"{video_id}.%(ext)s"), url],
                              capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"yt-dlp 运行失败：{str(exc)[:200]}"
    if done.returncode != 0:
        return None, f"yt-dlp 下载失败：{(done.stderr or done.stdout or '').strip()[-200:]}"
    return _finish_file(folder, video_id), ""


def _find_play_url(page):
    match = re.search(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', page, re.S)
    if match:
        try:
            data = json.loads(match.group(1))
            item = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
            video = item.get("video") or {}
            for key in ("playAddr", "downloadAddr"):
                if video.get(key):
                    return video[key]
            for bitrate in video.get("bitrateInfo") or []:
                urls = ((bitrate.get("PlayAddr") or {}).get("UrlList") or [])
                if urls:
                    return urls[0]
        except (ValueError, KeyError, TypeError):
            pass
    match = re.search(r'"playAddr":"([^"]+)"', page)
    if match:
        return json.loads(f'"{match.group(1)}"')
    return None


def _with_page(url, folder, video_id):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            page = response.read(5 * 1024 * 1024).decode("utf-8", "replace")
        play = _find_play_url(page)
        if not play:
            return None, "TikTok 页面里没找到视频地址（视频可能已删除、私密或需要登录）"
        target = folder / f"{video_id}.mp4"
        request = urllib.request.Request(play, headers={**headers, "Referer": "https://www.tiktok.com/"})
        with opener.open(request, timeout=60) as response, target.open("xb") as output:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise DownloadError("视频超过 2GB。")
                output.write(chunk)
    except DownloadError:
        raise
    except Exception as exc:
        return None, f"从 TikTok 页面下载失败：{str(exc)[:200]}"
    return _finish_file(folder, video_id), ""


def download_tiktok(url, folder, video_id):
    """Return the downloaded file inside `folder`; raise DownloadError with every attempt's reason."""
    reasons = []
    for attempt in (_with_yt_dlp_module, _with_yt_dlp_exe, _with_page):
        for leftover in folder.iterdir():
            if leftover.is_file():
                leftover.unlink()
        path, reason = attempt(url, folder, video_id)
        if path:
            return path
        reasons.append(reason or "未取得视频文件")
    raise DownloadError("原视频没有下载成功。" + "；".join(reasons) + "。可以在案例详情手动导入视频，或在电脑上安装 yt-dlp（py -m pip install yt-dlp）后点「重试」。")


MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,79}")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


class AutoBreakdown:
    def __init__(self, app, analyzer=None, downloader=None):
        self.app = app
        self.root = Path(app.runtime) / "evidence" / "auto-breakdown"
        self.analyzer = analyzer or pending_analyzer
        self.downloader = downloader or download_tiktok
        self.lock = threading.Lock()
        self.threads = {}
        self.download_slots = threading.Semaphore(PARALLEL_DOWNLOADS)
        self.analysis_slots = threading.Semaphore(PARALLEL_ANALYSES)
        self.tags = CaseTags(app.runtime)

    # ---- what the breakdown is given ---------------------------------
    def _videos(self):
        try:
            return self.app.performance.videos(self.app.video_case_links())
        except Exception:
            return []

    def insights(self, case_id, comment_limit=8):
        """Funnel position, contrast videos, comments, transcript gate and structured tags for one case."""
        case = self.app.store.read_case(case_id)
        current = case.get("current") or {}
        video_id = str((current.get("metrics") or {}).get("video_id") or "").strip()
        derived = self.tags.derive(case, self.app.store.root)
        result = {"case_id": case_id, "video_id": video_id, "transcript": transcript_gate(case),
                  "funnel": None, "contrasts": [], "comments": None, "missing_video_id": not video_id}
        if video_id:
            videos = self._videos()
            video = next((v for v in videos if v.get("video_id") == video_id), None)
            if video:
                result["funnel"] = funnel(video, videos)
                result["contrasts"] = contrasts(video, videos)
                derived["funnel"] = result["funnel"]["key"]
            result["comments"] = comments(self.app.reviews, video_id, limit=max(comment_limit, 1))
        result["tags"] = self.tags.update(case_id, derived, "frontmatter") if derived else self.tags.get(case_id)
        return result

    def batch_collect(self, video_ids):
        """Create a case for each uncollected video (highest orders first) and queue its download and breakdown."""
        if not isinstance(video_ids, list) or not video_ids:
            raise ValueError("请先选择要收为案例的视频。")
        if len(video_ids) > BATCH_LIMIT:
            raise ValueError(f"一次最多收 {BATCH_LIMIT} 条，免得 AI 额度一下子用完。")
        videos = {v["video_id"]: v for v in self._videos()}
        links = self.app.video_case_links()
        results = []
        for raw in video_ids:
            video_id = str(raw or "").strip()
            video = videos.get(video_id)
            if not VIDEO_ID.fullmatch(video_id) or not video:
                results.append({"video_id": video_id, "status": "skipped", "message": "后台数据里没有这条视频。"})
                continue
            if links.get(video_id):
                results.append({"video_id": video_id, "status": "skipped", "case_id": links[video_id], "message": "已经收为案例。"})
                continue
            try:
                case_id = self._create_case(video)
                links[video_id] = case_id
                job = self.start(case_id, video_id=video_id)
                results.append({"video_id": video_id, "status": "queued", "case_id": case_id, "run_id": job["run_id"]})
            except Exception as exc:
                results.append({"video_id": video_id, "status": "failed", "message": str(exc)[:300]})
        return results

    def _create_case(self, video):
        orders = int(video.get("orders") or 0)
        views = int(video.get("views") or 0)
        account = str(video.get("account") or "").lstrip("@")
        metrics = {"video_id": video["video_id"]}
        if account:
            metrics["creator_handle"] = "@" + account
        if video.get("title"):
            metrics["published_title"] = str(video["title"])
        current = {"title": str(video.get("headline") or video["video_id"]), "category": "批量收录 · 待拆解",
                   "conclusion": f"从视频数据批量收录：累计 {orders} 单、{views:,} 播放；拆解完成前不作结论。",
                   "kind": "performance", "content_origin": "self_produced", "metrics": metrics,
                   "assessments": [{"dimension": "conversion", "polarity": "positive" if orders >= 10 else "unknown",
                                    "reason": f"后台累计 {orders} 单"}],
                   "evidence": {"status": "source_checked", "source": "TikTok 后台视频数据导出"}, "assets": {}}
        if video.get("posted_on"):
            current["publish"] = {"published_on": str(video["posted_on"])[:10]}
        body = {"current": current, "product": str(video.get("products") or video.get("product") or "未记录产品"),
                "url": f"https://www.tiktok.com/@{account or '_'}/video/{video['video_id']}",
                "note": "从视频数据批量收为案例", "request_id": "batch-" + uuid4().hex}
        result = self.app.save(None, body)
        return result["case"]["case_id"]

    # ---- job records -------------------------------------------------
    def _path(self, case_id):
        if not CASE_ID.fullmatch(str(case_id)):
            raise ValueError("案例编号无效。")
        return self.root / f"{case_id}.json"

    def _read(self, case_id):
        path = self._path(case_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None

    def _write(self, job):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(job["case_id"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _update(self, job, **fields):
        job.update(fields, updated_at=now())
        job["label"] = STEP_LABELS.get(job["status"], job["status"])
        self._write(job)
        status = {"completed": "completed", "failed": "failed", "interrupted": "interrupted",
                  "analyzer_pending": "needs_attention"}.get(job["status"], "running")
        try:
            activity = getattr(self.app, "activity", None)
            if activity is not None:
                activity.record(job["run_id"], kind="case", owner_id=job["case_id"], source_id=job["run_id"],
                                product=job.get("product", ""), title=f"自动拆解 · {job.get('title') or job['case_id']}",
                                case_id=job["case_id"], status=status, phase=job["label"], message=job.get("message", ""),
                                event_key=f"{job['status']}:{job['updated_at']}" if status != "running" else None)
        except Exception:
            pass  # The task panel only observes; the job record above is the source of truth.
        return job

    def status(self, case_id):
        job = self._read(case_id)
        if job and job["status"] in ACTIVE:
            thread = self.threads.get(case_id)
            if not thread or not thread.is_alive():
                job = self._update(job, status="interrupted", message="工作台重启时这项任务还没做完，点「重试」继续。")
        return job

    def resume_pending(self):
        """Rerun every job left at 「拆解待接入」 or cut off by a restart, once a real analyzer is wired in."""
        if self.analyzer is pending_analyzer or not self.root.is_dir():
            return []
        resumed = []
        for path in sorted(self.root.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            structure = job.get("status") == "failed" and str(job.get("message") or "").startswith("拆解两次都没通过") and not job.get("resumed_after_structure")
            if job.get("status") not in {"analyzer_pending", "interrupted", *ACTIVE} and not structure:
                continue
            thread = self.threads.get(job.get("case_id"))
            if thread and thread.is_alive():
                continue
            try:
                started = self.start(job["case_id"], video_id=job.get("video_id"), retry=True,
                                     model=job.get("model"), reasoning_effort=job.get("reasoning_effort"))
                if structure:
                    started["resumed_after_structure"] = True  # one automatic rerun per validation failure
                resumed.append(job["case_id"])
            except Exception as exc:
                try:
                    self._update(job, status="failed", message=f"自动补跑没能开始：{str(exc)[:200]}")
                except Exception:
                    pass
        return resumed

    # ---- running -----------------------------------------------------
    def start(self, case_id, *, video_id=None, retry=False, model=None, reasoning_effort=None):
        model = str(model or "").strip() or None
        reasoning_effort = str(reasoning_effort or "").strip() or None
        if model and not MODEL_ID.fullmatch(model):
            raise ValueError("模型名称无效。")
        if reasoning_effort and reasoning_effort not in EFFORTS:
            raise ValueError("思考强度无效。")
        case = self.app.store.read_case(case_id)
        current = case.get("current") or {}
        video_id = str(video_id or (current.get("metrics") or {}).get("video_id") or "").strip()
        url = str((case.get("original") or {}).get("url") or "")
        link = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", url)
        if not video_id and link:
            video_id = link.group(1)
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError("这条案例没有关联 TikTok 视频 ID，无法自动下载。")
        if not link or link.group(1) != video_id:
            url = f"https://www.tiktok.com/@_/video/{video_id}"
        with self.lock:
            thread = self.threads.get(case_id)
            if thread and thread.is_alive():
                return self._read(case_id)
            previous = self._read(case_id)
            if previous and previous["status"] == "completed" and not retry:
                return previous
            job = {"case_id": case_id, "run_id": "ABD-" + uuid4().hex[:12], "video_id": video_id, "url": url,
                   "product": case.get("product", ""), "title": current.get("title") or case.get("title", ""),
                   "status": "queued", "message": "", "video_source": "", "video_path": "",
                   "analysis": None, "started_at": now(), "attempt": (previous or {}).get("attempt", 0) + 1,
                   # None = the global default; a manual 「AI 拆解」 can pick another model for this run.
                   "model": model, "reasoning_effort": reasoning_effort}
            self._update(job)
            thread = threading.Thread(target=self._run, args=(job,), name=f"auto-breakdown-{case_id}", daemon=True)
            self.threads[case_id] = thread
            thread.start()
            return job

    def _local_publication(self, video_id):
        """A finished file already bound to a publication with this video ID, if any."""
        try:
            store = self.app._video_publications()
            data = store.read()
        except Exception:
            return None
        for entry in data.get("jobs", {}).values():
            for record in [entry.get("publication"), *entry.get("history", [])]:
                if record and record.get("file") and str(record.get("post_id") or "") == video_id:
                    try:
                        path = store.managed_path(record["file"])
                        if path.is_file():
                            return path
                    except Exception:
                        continue
        return None

    def _attach(self, job, source):
        case = self.app.store.read_case(job["case_id"])
        base = case_revision(case)  # before editing the snapshot below
        current = copy.deepcopy(case.get("current") or {})
        existing = (current.get("assets") or {}).get("video")
        if existing and Path(existing).is_file():
            return Path(existing), False
        current.setdefault("assets", {})["video"] = str(source)
        self.app.save(job["case_id"], {"current": current, "revision": base,
                                                "note": "自动下载原视频并关联到案例", "request_id": "auto-" + uuid4().hex})
        saved = self.app.store.read_case(job["case_id"])
        return Path(((saved.get("current") or {}).get("assets") or {})["video"]), True

    def _wait(self, slots, job, message, status="queued"):
        if not slots.acquire(blocking=False):
            self._update(job, status=status, message=message)
            slots.acquire()

    def _run(self, job):
        case_id = job["case_id"]
        staged_folder = None
        try:
            case = self.app.store.read_case(case_id)
            existing = ((case.get("current") or {}).get("assets") or {}).get("video")
            if existing and Path(existing).is_file():
                video_path = Path(existing)
                self._update(job, status="analyzing", video_source="案例已有视频", video_path=str(video_path))
            else:
                self._wait(self.download_slots, job, f"前面有 {PARALLEL_DOWNLOADS} 条在下载，轮到后自动开始。")
                try:
                    self._update(job, status="downloading", message="先查本机已登记的成片，没有再从 TikTok 下载。")
                    source = self._local_publication(job["video_id"])
                    if source:
                        job["video_source"] = "本机已登记成片"
                    else:
                        staged_folder = self.app.video_files._new_folder(self.app.video_files.imports)
                        source = self.downloader(job["url"], staged_folder, job["video_id"])
                        job["video_source"] = "从 TikTok 下载"
                    self._update(job, status="attaching", message="")
                    video_path, _ = self._attach(job, source)
                finally:
                    self.download_slots.release()
                self._update(job, status="analyzing", video_path=str(video_path), message="")
            try:
                job["inputs"] = self.insights(case_id, comment_limit=40)
            except Exception as exc:
                job["inputs"] = {"error": f"拆解定位没算出来：{str(exc)[:200]}"}
            self._update(job)
            self._wait(self.analysis_slots, job, f"前面有 {PARALLEL_ANALYSES} 条在拆解，轮到后自动开始。", status="analyzing")
            try:
                analysis = self.analyzer(self.app, case_id, video_path, job)
            except AnalyzerPending as pending:
                self._update(job, status="analyzer_pending", message=str(pending))
                return
            finally:
                self.analysis_slots.release()
            if isinstance(analysis, dict) and isinstance(analysis.get("tags"), dict):
                self.tags.update(case_id, analysis["tags"], "analysis")
            self._update(job, status="completed", analysis=analysis, message="拆解已写入案例。")
        except Exception as exc:
            message = str(exc) if isinstance(exc, (DownloadError, ValueError)) else f"处理失败：{str(exc)[:300]}"
            self._update(job, status="failed", message=message)
        finally:
            if staged_folder is not None:
                # Saving copied the video into the case folder; anything left here is unreferenced.
                try:
                    for leftover in staged_folder.iterdir():
                        if leftover.is_file():
                            leftover.unlink()
                    staged_folder.rmdir()
                except OSError:
                    pass
