"""Bounded TikTok comment intake into a durable, rebuildable raw source."""
from __future__ import annotations

import html
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE_NAME = "_tk_plugin_comments.json"
MAX_VIDEOS = 200
MAX_COMMENTS = 10000
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_TEXT = 10000


class CommentIntakeError(ValueError):
    pass


def _text(value, limit, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit:
        raise CommentIntakeError("评论字段格式或长度无效。")
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value).replace("\u00a0", " ").strip()
    if required and not value:
        raise CommentIntakeError("评论缺少正文。")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{6,25}", value):
        raise CommentIntakeError("评论或视频编号无效。")
    return value


def _created(value):
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        raise CommentIntakeError("评论时间无效。")
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{9,13}", value)):
            stamp = float(value)
            if not math.isfinite(stamp) or stamp <= 0:
                raise ValueError()
            moment = datetime.fromtimestamp(stamp / 1000 if stamp >= 10 ** 11 else stamp, timezone.utc)
            return moment.isoformat().replace("+00:00", "Z")
        text = _text(value, 80)
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return text
    except (ValueError, OverflowError, OSError):
        raise CommentIntakeError("评论时间无效。") from None


def source_record(record):
    """The parser's normalized record, reduced to the source fields we retain."""
    return {"cid": str(record["native"]), "videoWebUrl": record.get("url") or "", "text": record.get("text") or "",
            "diggCount": record.get("likes"), "uniqueId": record.get("author_handle") or "", "nickname": record.get("author_name") or "",
            "createTimeISO": record.get("created_at") or record.get("posted_on") or "", "repliesToId": record.get("parent_native"),
            "product": record.get("product", "202H"), "videoAuthor": record.get("video_author") or ""}


def _read(path):
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list) or any(not isinstance(row, dict) or not row.get("cid") for row in data):
            raise ValueError()
        return {str(row["cid"]): row for row in data}
    except (OSError, ValueError):
        raise CommentIntakeError("插件评论来源文件无法读取；原文件已保留，请先修复后再推送。") from None


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".plugin-comments-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump([records[key] for key in sorted(records)], stream, ensure_ascii=False, indent=1, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ingest(path, videos, products, existing):
    """Merge comments by native cid. All result counts describe comments, not videos.

    products is the workbench's authoritative video-id -> product map. existing
    contains normalized TikTok records from all sources; a partial capture never
    removes comments or replaces an established author with an empty value.
    Caller serializes this write with other review-source mutations.
    """
    if not isinstance(videos, list) or not videos or len(videos) > MAX_VIDEOS or not isinstance(products, dict):
        raise CommentIntakeError(f"评论推送应包含 1 至 {MAX_VIDEOS} 条视频。")
    try:
        if len(json.dumps(videos, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise CommentIntakeError("本次评论推送过大，请分批推送。")
    except (TypeError, ValueError):
        raise CommentIntakeError("评论推送格式或大小无效。") from None
    if any(not isinstance(video, dict) or not isinstance(video.get("comments"), list) for video in videos):
        raise CommentIntakeError("每条视频的 comments 应为评论列表。")
    if sum(len(video["comments"]) for video in videos) > MAX_COMMENTS:
        raise CommentIntakeError(f"一次最多推送 {MAX_COMMENTS} 条评论。")
    path = Path(path)
    stored = _read(path)
    current = {str(row["native"]): source_record(row) for row in existing}
    result = {"saved": 0, "updated": 0, "unchanged": 0, "invalid": 0, "ignored": 0, "ignored_ids": []}
    changed = False
    for video in videos:
        try:
            video_id = _id(video.get("id"))
            video_author = _text(video.get("author"), 100).lstrip("@")
        except CommentIntakeError:
            result["invalid"] += len(video["comments"]) or 1
            continue
        product = products.get(video_id)
        if video_id not in products or not isinstance(product, str) or len(product) > 200:
            result["ignored"] += len(video["comments"])
            if video_id not in result["ignored_ids"] and len(result["ignored_ids"]) < 20:
                result["ignored_ids"].append(video_id)
            continue
        for comment in video["comments"]:
            try:
                if not isinstance(comment, dict):
                    raise CommentIntakeError("评论格式无效。")
                identifier = _id(comment.get("id"))
                if comment.get("videoId") not in (None, "", video_id):
                    raise CommentIntakeError("评论与视频编号不匹配。")
                previous = current.get(identifier)
                if previous and not re.search(r"/video/" + re.escape(video_id) + r"(?:[/?#]|$)", previous["videoWebUrl"]):
                    raise CommentIntakeError("评论编号已属于另一条视频。")
                text = _text(comment.get("text"), MAX_TEXT, required=True)
                author_name = _text(comment.get("authorName"), 200)
                author_handle = _text(comment.get("authorHandle"), 100).lstrip("@")
                created = _created(comment.get("createdAt"))
                parent = comment.get("parentId")
                parent = _id(parent) if parent not in (None, "", "0", 0) else None
                if parent == identifier:
                    raise CommentIntakeError("评论不能回复自身。")
                likes = comment.get("likes")
                if likes is not None and (isinstance(likes, bool) or not isinstance(likes, int) or not 0 <= likes <= 10 ** 13):
                    raise CommentIntakeError("评论点赞数无效。")
                candidate = dict(previous or {"cid": identifier, "diggCount": None, "uniqueId": "", "nickname": "",
                    "createTimeISO": "", "repliesToId": None, "videoAuthor": ""})
                candidate.update(text=text, product=product.strip())
                if video_author or not candidate.get("videoWebUrl"):
                    from urllib.parse import quote
                    candidate["videoWebUrl"] = f"https://www.tiktok.com/@{quote(video_author or '_', safe='')}/video/{video_id}"
                for key, value in (("nickname", author_name), ("uniqueId", author_handle), ("createTimeISO", created),
                                   ("repliesToId", parent), ("videoAuthor", video_author)):
                    if value:
                        candidate[key] = value
                if likes is not None:
                    candidate["diggCount"] = likes
            except CommentIntakeError:
                result["invalid"] += 1
                continue
            if previous == candidate:
                result["unchanged"] += 1
                continue
            result["updated" if previous else "saved"] += 1
            current[identifier] = stored[identifier] = candidate
            changed = True
    if changed:
        _write(path, stored)
    return result
