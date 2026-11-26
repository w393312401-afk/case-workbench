"""User comments on a picture carousel, shown like a TikTok comment sticker with the commenter's
own name and picture. At 选版 the AI only suggests search words in two groups; the user can add or remove words, the
words search every usable synced TikTok comment of this video and of the same product (here, no AI), and the user picks:

- cover (首图), one: a short doubt, question or relatable struggle; it goes on the cover as a sticker over its picture.
- middle (中间), at most one: real-use proof; it becomes a slide of its own, second to last, right before the closing
  slide, with a title the user writes under it.

The quoted words always come from the review library: emoji are left out, nothing else is changed; comments that
@mention someone else are not offered.
"""
from __future__ import annotations

import math
import re

# Archived 2026-11-19 at the user's request: the 选版 comments block is hidden, nothing is suggested or put into new
# versions, and the comment endpoints refuse. Code and saved data stay; set to False to bring it back.
ARCHIVED = True

MAX_WORDS = 12  # search words per group
SHOW = 30  # matches shown per group
COVER_WORDS = 14  # a cover sticker is read in a second: about 12 words, two lines at most
SLOTS = {"cover": "首图", "middle": "中间"}
MIN_CHARS, MAX_CHARS = 15, 180
CONCERN = ("objection", "defect", "question", "compare")
USE = ("owner", "result", "advice")
SKIP_ONLY = {"noise", "buy_info"}
KINDS = {"concern": "顾虑", "use": "使用反馈"}
_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿⬀-⯿️‍⃣]")
_CJK = re.compile("[㐀-鿿豈-﫿]")


def display_text(text):
    """The words as they will be quoted: emoji dropped (the slide fonts cannot draw them), spaces tidied."""
    return re.sub(r"\s+", " ", _EMOJI.sub("", str(text or ""))).strip()


def kind_of(tags):
    nature = set((tags or {}).get("nature") or [])
    if nature & set(CONCERN):
        return "concern"
    if nature & set(USE):
        return "use"
    return ""


def video_posts(app, job_id, timeline_uid):
    """TikTok post ids registered for this timeline (its own comments are the closest match)."""
    reader = getattr(app, "_video_publications", None)
    if not callable(reader):
        return []
    try:
        from workbench_video_publish import VideoPublicationStore, timeline_records
        entry = VideoPublicationStore.entry(reader().read(), job_id)
        return list(dict.fromkeys(str(r["post_id"]) for r in timeline_records(entry, timeline_uid) if r.get("post_id")))
    except Exception:
        return []


def candidates(app, data, limit=None):
    """Every comment that could go on a slide, best first: under this video, then under any video of the same product.
    English only, no brand replies, no @mentions, short enough for one sticker, and saying something (not noise or
    'how much')."""
    reviews = getattr(app, "reviews", None)
    if reviews is None:
        return []
    posts = video_posts(app, data.get("job_id"), data.get("timeline_uid"))
    queries = [({"platform": ["tiktok_video"], "video": post, "language": "en"}, True) for post in posts]
    if data.get("product"):
        queries.append(({"platform": ["tiktok_video"], "product": data["product"], "language": "en"}, False))
    found = {}
    for query, own in queries:
        try:
            rows = reviews.search(query, limit=2000, sort="likes")["reviews"]
        except Exception:
            continue
        for row in rows:
            if row["id"] in found:
                continue
            text = display_text(row.get("text"))
            nature = set(((row.get("tags") or {}).get("nature")) or [])
            if (not MIN_CHARS <= len(text) <= MAX_CHARS or len(text.split()) < 3 or "@" in text
                    or (nature and nature <= SKIP_ONLY)):
                continue
            kind = kind_of(row.get("tags"))
            score = ((100 if own else 0) + (20 if kind else 0) + (5 if "praise" in nature else 0) + (5 if row.get("avatar") else 0)
                     + math.log1p(row.get("likes") or 0) * 3)
            name = re.sub(r"\s+", " ", _EMOJI.sub("", str(row.get("author_handle") or ""))).strip().lstrip("@")
            name = f"@{name}" if name and " " not in name else display_text(row.get("author_name"))
            found[row["id"]] = {"id": row["id"], "text": text, "username": name, "avatar": row.get("avatar") or None, "likes": row.get("likes") or 0, "kind": kind,
                                "nature": sorted(nature), "same_video": own, "video_id": row.get("source_ref") or "",
                                "posted_on": row.get("posted_on") or "", "score": score, "zh": row.get("zh") or ""}
    found = sorted(found.values(), key=lambda item: -item["score"])
    return found[:limit] if limit else found


def clean_words(words):
    """Search words as the user or the AI gave them: trimmed, lower case, no repeats, at most MAX_WORDS."""
    out = []
    for word in words or []:
        word = re.sub(r"\s+", " ", str((word or {}).get("word") if isinstance(word, dict) else word or "")).strip().lower()[:40]
        if word and word not in out:
            out.append(word)
    return out[:MAX_WORDS]


def _patterns(word):
    """A Chinese word stands for the English words it is usually written as (the comments are English)."""
    if _CJK.search(word):
        try:
            from review_taxonomy import zh_groups
            terms = [term for _, group in zh_groups(word) for term in group]
        except Exception:
            terms = []
    else:
        terms = [word]
    out = []
    for term in terms:
        body = re.escape(term.lower()).replace(r"\ ", r"\s+")
        out.append(re.compile((r"\b" if term[:1].isalnum() else "") + body + (r"\b" if term[-1:].isalnum() else ""), re.I))
    return out


def search(pool, words, slot, limit=SHOW):
    """The comments that contain any of the words, the ones matching more words first, then the usual order (this
    video, has a picture, likes). A cover sticker longer than COVER_WORDS is not offered for the cover."""
    patterns = [(word, _patterns(word)) for word in clean_words(words)]
    hits = []
    for item in pool:
        if slot == "cover" and len(item["text"].split()) > COVER_WORDS:
            continue
        # A Chinese word also finds the comment through its Chinese translation.
        matched = [word for word, found in patterns if any(p.search(item["text"]) for p in found)
                   or (_CJK.search(word) and word in (item.get("zh") or ""))]
        if matched:
            hits.append({k: v for k, v in item.items() if k != "score"} | {"matched": matched, "_rank": (-len(matched), -item["score"])})
    hits.sort(key=lambda hit: hit.pop("_rank"))
    return hits[:limit], len(hits)


def keyword_prompt(data, versions, pool, refs, script, note=""):
    covers = "\n".join(f"  {v['key']} 版：{' / '.join(((v.get('slides') or [{}])[0]).get('title_lines') or [])}" for v in versions)
    sample = "\n".join(f"- {item['text']}" for item in pool[:80])
    return f"""给这组 TikTok 挂车图文找用户评论用的搜索关键词。系统会用关键词在已同步的全部评论里搜，用户自己从搜出来的评论里挑，所以你只给关键词，不挑评论。

分两组，每组 5–8 个英文词或短语（评论是英文的，用美国用户评论里真会出现的说法，越短越容易搜到）：
一、cover（首图钩子）：能让刷到的人停下来的质疑、提问、处境，如 really work、worth it、does it、always、can't、struggle、how do you；也包括这个产品特有的顾虑词。
二、middle（真实使用反馈）：买了、用了、用出结果的说法，如 bought、been using、weeks、still sharp、my dad、gift。

不要给品牌名、竞品名、脏话；每个词配一句中文意思。

产品型号：{data.get('product') or '未登记'}。产品资料：{'、'.join(refs) or '无'}。

视频脚本（英文｜中文）：
{script}

各版首图标题：
{covers}

评论库里这个产品的部分评论（让你知道大家怎么说）：
{sample}
{('补充说明：' + note) if note else ''}

只输出一个 JSON 对象，不要别的文字：
{{"cover":[{{"word":"really work","zh":"真的有用吗"}}],"middle":[{{"word":"been using","zh":"用了一段时间"}}]}}"""


def parse_keywords(output):
    from photo_carousel import PhotoSetError, _json_answer
    data = _json_answer(output, "AI 没按约定格式交回关键词，可以再点一次。")
    out = {}
    for slot in SLOTS:
        rows, seen = [], set()
        for item in data.get(slot) or []:
            word = clean_words([item])
            if word and word[0] not in seen:
                seen.add(word[0])
                rows.append({"word": word[0], "zh": str((item or {}).get("zh") or "")[:40] if isinstance(item, dict) else ""})
        out[slot] = rows[:MAX_WORDS]
    if not any(out.values()):
        raise PhotoSetError(502, "AI 没给出关键词，可以再点一次，或者自己填。")
    return out


def _image_time(slide):
    image = slide.get("image") or {}
    return float(image["t"]) if image.get("kind") == "frame" and image.get("t") is not None else None


def pick_frame(slides, near, items):
    """A picture for the comment slide that no other slide uses: the AI's backup frames of the slide it follows (or the
    next one), else the sharpest unused frame."""
    used = [t for s in slides for t in [_image_time(s), *[_image_time(c) for c in s.get("cells") or []]] if t is not None]
    free = lambda t: all(abs(t - u) >= 0.6 for u in used)
    for index in (near - 1, near):
        if 0 <= index < len(slides):
            for t in slides[index].get("alternates") or []:
                if free(float(t)):
                    return float(t)
    for item in sorted(items, key=lambda item: -item["sharp"]):
        if free(item["t"]):
            return item["t"]
    return _image_time(slides[max(0, near - 1)]) or 0.0


def quote(pick, **extra):
    return {"review_id": pick["id"], "text": pick["text"], "zh": pick.get("zh", ""), "kind": pick.get("kind", ""),
            "username": pick.get("username") or "", "avatar": pick.get("avatar"), **extra}


def inserted(slide):
    """A slide that exists only to carry a comment (the cover with a sticker on it is still the version's own slide)."""
    return bool(slide.get("comment")) and not slide["comment"].get("cover")


def comment_slide(pick, time):
    """The middle comment's own slide: the sticker over a picture; the title under it is left for the user to write."""
    return {"title_lines": [], "body_lines": [], "title_zh": "", "body_zh": "",
            "picture_zh": f"用户评论：{pick.get('zh') or pick['text']}", "crop_y": 0.5, "text_position": "bottom", "text_size": "m",
            "image": {"kind": "frame", "t": time}, "layout": "single", "cells": [],
            "comment": quote(pick)}


def place(slides, picks, key, items, max_slides):
    """Put the chosen comments into one version. Returns (slides, placed, skipped). The cover pick goes on slide 1 as a
    sticker over its own picture and title (replacing a sticker from another comment); the middle pick becomes a slide of
    its own, second to last, never first and never last."""
    slides = list(slides)
    placed, skipped = [], []
    for pick in picks:
        if pick.get("slot") == "cover":
            if not slides:
                skipped.append(pick["id"])
                continue
            if (slides[0].get("comment") or {}).get("review_id") == pick["id"]:
                continue
            slides[0] = {**slides[0], "comment": quote(pick, cover=True)}
            placed.append(pick["id"])
            continue
        if any((s.get("comment") or {}).get("review_id") == pick["id"] for s in slides):
            continue
        if len(slides) < 2 or len(slides) >= max_slides:
            skipped.append(pick["id"])
            continue
        at = len(slides) - 1  # second to last, right before the closing slide
        slides.insert(at, comment_slide(pick, pick_frame(slides, at, items)))
        placed.append(pick["id"])
    return slides, placed, skipped
