"""开头库：对标视频的前 3 秒（口播、屏显、第一帧、开头手法）连同那条视频的公开数据。

TK 看板插件从「已保存的结果」推送过来，按视频 ID 一条记录，后推的覆盖先推的，人工改过的开头手法保留。
记录存运行目录 `hook-bank/hooks.json`，第一帧存 `hook-bank/frames/<视频ID>.jpg`。
"""

import base64
import binascii
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

# 开头手法分类。插件的 AI 拆解给的是自由文字（如「结果先行/痛点提问」），按关键词归到这几类；归不进的算「其他」，
# 原文保留在 type_raw，页面上可以手动改。顺序即判断先后：质疑类常被写成「反常识」或「提问」，先认它。
TYPES = ("质疑回应", "结果先行", "痛点提问", "身份点名", "反常识", "对比开场", "演示开场", "直接报产品", "其他")
KEYWORDS = (
    ("质疑回应", ("质疑", "怀疑", "反驳", "辟谣", "打假", "骗", "传言", "真的吗", "回应")),
    ("结果先行", ("结果", "效果", "前后", "成品", "先看")),
    ("反常识", ("反常识", "颠覆", "没想到", "误区", "冷知识")),
    ("对比开场", ("对比", "比较")),
    ("身份点名", ("身份", "点名", "人群", "喊话")),
    ("痛点提问", ("痛点", "提问", "问题", "烦恼", "提问式")),
    ("演示开场", ("演示", "开箱", "展示", "操作")),
    ("直接报产品", ("产品名", "报产品", "直接介绍", "品牌", "直接")),
)
VIDEO_ID = re.compile(r"\d{6,25}")
MAX_FRAME = 400 * 1024
MAX_VIDEOS = 50


class HookBankError(ValueError):
    pass


def classify(raw):
    text = str(raw or "")
    for name, words in KEYWORDS:
        if any(word in text for word in words):
            return name
    return "其他"


def _text(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and value == value and value != float("inf") else None


def _clean_stats(stats):
    if not isinstance(stats, dict):
        return {}
    out = {key: number for key in ("views", "likes", "comments", "shares", "followers") if (number := _number(stats.get(key))) is not None}
    published = stats.get("publishedAt")
    if isinstance(published, str) and len(published) <= 40:
        try:
            datetime.fromisoformat(published.replace("Z", "+00:00"))
            out["published_at"] = published
        except ValueError:
            pass
    if isinstance(stats.get("commerce"), bool):
        out["commerce"] = stats["commerce"]
    return out


def _clean_lines(lines):
    out = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        item = {"time": _text(line.get("time"), 12), "en": _text(line.get("en"), 300), "zh": _text(line.get("zh"), 300)}
        if item["en"] or item["zh"]:
            out.append(item)
        if len(out) == 5:
            break
    return out


def _frame_bytes(value):
    """A small JPEG from the plugin, as a data URL. Anything else is ignored rather than refused: the hook still counts."""
    if not isinstance(value, str) or not value.startswith("data:image/jpeg;base64,") or len(value) > MAX_FRAME * 4 // 3 + 64:
        return None
    try:
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if raw[:3] == b"\xff\xd8\xff" and len(raw) <= MAX_FRAME else None


class HookBank:
    def __init__(self, runtime_root):
        self.root = Path(runtime_root) / "hook-bank"
        self.path = self.root / "hooks.json"
        self.frames = self.root / "frames"
        self.lock = threading.Lock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise HookBankError("开头库文件读不出来，请先检查运行目录 hook-bank/hooks.json。") from error
        return data.get("hooks", {}) if isinstance(data, dict) and isinstance(data.get("hooks"), dict) else {}

    def _write(self, hooks):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.root, prefix="hooks-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "hooks": hooks}, handle, ensure_ascii=False, indent=1)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def save(self, product, videos, now=None):
        if not isinstance(product, str) or not product.strip() or len(product) > 200:
            raise HookBankError("请选择产品。")
        if not isinstance(videos, list) or not videos or len(videos) > MAX_VIDEOS:
            raise HookBankError(f"一次存 1–{MAX_VIDEOS} 条视频的开头。")
        stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved = updated = invalid = 0
        with self.lock:
            hooks = self._read()
            for video in videos:
                if not isinstance(video, dict) or not isinstance(video.get("id"), str) or not VIDEO_ID.fullmatch(video["id"]):
                    invalid += 1
                    continue
                hook = video.get("hook") if isinstance(video.get("hook"), dict) else {}
                lines = _clean_lines(hook.get("lines"))
                if not lines:
                    invalid += 1
                    continue
                video_id = video["id"]
                before = hooks.get(video_id) or {}
                type_raw = _text(hook.get("type"), 40)
                record = {
                    "video_id": video_id, "product": product.strip(),
                    "author": _text(video.get("author"), 100), "title": _text(video.get("title"), 300),
                    "stats": _clean_stats(video.get("stats")) or before.get("stats") or {},
                    "lines": lines, "screen": _text(hook.get("screen"), 200), "why": _text(hook.get("why"), 300),
                    "type_raw": type_raw, "type": before.get("type") if before.get("type_manual") else classify(type_raw),
                    "type_manual": bool(before.get("type_manual")),
                    "saved_at": before.get("saved_at") or stamp, "updated_at": stamp,
                    "frame": bool(before.get("frame")),
                }
                frame = _frame_bytes(video.get("frame"))
                if frame:
                    self.frames.mkdir(parents=True, exist_ok=True)
                    (self.frames / f"{video_id}.jpg").write_bytes(frame)
                    record["frame"] = True
                hooks[video_id] = record
                if before:
                    updated += 1
                else:
                    saved += 1
            if saved or updated:
                self._write(hooks)
        return {"ok": True, "saved": saved, "updated": updated, "invalid": invalid}

    def list(self, product=None):
        with self.lock:
            hooks = self._read()
        wanted = (product or "").strip().lower()
        items = [dict(item) for item in hooks.values() if not wanted or str(item.get("product", "")).lower() == wanted]
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def set_type(self, video_id, kind):
        if kind not in TYPES:
            raise HookBankError("开头手法不在列表里。")
        with self.lock:
            hooks = self._read()
            if video_id not in hooks:
                raise HookBankError("开头库里没有这条视频。")
            hooks[video_id].update(type=kind, type_manual=True)
            self._write(hooks)
        return {"ok": True, "video_id": video_id, "type": kind}

    def delete(self, video_id):
        with self.lock:
            hooks = self._read()
            if hooks.pop(video_id, None) is None:
                raise HookBankError("开头库里没有这条视频。")
            self._write(hooks)
            (self.frames / f"{video_id}.jpg").unlink(missing_ok=True)
        return {"ok": True, "video_id": video_id}

    def frame_path(self, video_id):
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            return None
        path = self.frames / f"{video_id}.jpg"
        return path if path.is_file() else None
