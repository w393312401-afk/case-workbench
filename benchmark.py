"""对标约束：TK 看板插件推来的对照拆解结果，写稿时勾选成硬条件，写完逐条核对。

对照拆解只列出圈组和对照组不一样的地方。用户在开始写稿时勾选其中几条（以及开头库里的一个参考开头），
它们冻结进写稿任务的 creation_context.benchmark；稿子写完后，后台用 AI 按这些条件逐条核对每条候选稿，
只报没做到的地方和最小改法。记录存运行目录 `benchmark/contrasts.json`、`benchmark/checks.json`。
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import markets
from direction_store import product_key

MAX_CONTRASTS = 200
MAX_CHECKS = 500

PROMPT_RULE = (
    "本轮按对标写稿：creation_context.benchmark.rules 是用户发起时勾选的硬条件，来自同品类出圈视频和没出圈视频的差别"
    "（basis 写着两组各有几条这样做）。每条候选稿都要做到每一条：画面类的写进对应句的画面要求，时间类的按配音语速排在要求的秒数以内。"
    "做不到或与产品事实冲突的，不硬凑，在稿外一行写明是哪一条、为什么。"
    "creation_context.benchmark.hook 有内容时，是用户从开头库挑的参考开头：照它的开头手法写本产品的开头，不照抄原句，"
    "原句里关于别的产品的事实一律换成本产品资料里有的事实。"
)


class BenchmarkError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _count(value):
    return value if type(value) is int and 0 <= value <= 1000 else None


def rule_text(diff):
    """What a writer has to do, in one line: the AI's writing advice, else what the 出圈 group did."""
    return diff.get("advice") or f"{diff['dim']}：{diff['hit']}"


def rule_basis(diff, hit=None, ctrl=None):
    parts = []
    if diff.get("hit_count") is not None and diff.get("hit_total"):
        parts.append(f"出圈 {diff['hit_count']}/{diff['hit_total']}")
    if diff.get("ctrl_count") is not None and diff.get("ctrl_total"):
        parts.append(f"对照 {diff['ctrl_count']}/{diff['ctrl_total']}")
    return " · ".join(parts) or (f"出圈 {hit} 条 vs 对照 {ctrl} 条" if hit is not None and ctrl is not None else "")


def rules_of(contrast):
    return [{"n": index, "dim": diff["dim"], "text": rule_text(diff), "basis": rule_basis(diff, contrast.get("hit"), contrast.get("ctrl")),
             "hit": diff["hit"], "ctrl": diff["ctrl"]}
            for index, diff in enumerate(contrast.get("diffs") or [], 1)]


def clean_contrast(payload):
    if not isinstance(payload, dict):
        raise BenchmarkError("对照结果格式错误。")
    diffs = []
    for raw in payload.get("diffs") if isinstance(payload.get("diffs"), list) else []:
        if not isinstance(raw, dict):
            continue
        diff = {"dim": _text(raw.get("dim"), 40), "measure": _text(raw.get("measure"), 80), "hit": _text(raw.get("hit"), 300),
                "ctrl": _text(raw.get("ctrl"), 300), "advice": _text(raw.get("advice"), 300),
                **{key: _count(raw.get(key)) for key in ("hit_count", "hit_total", "ctrl_count", "ctrl_total")},
                "evidence": [{"video": _text(item.get("video"), 20), "time": _text(item.get("time"), 20), "quote": _text(item.get("quote"), 160)}
                             for item in (raw.get("evidence") if isinstance(raw.get("evidence"), list) else [])[:4] if isinstance(item, dict)]}
        if diff["dim"] and (diff["hit"] or diff["advice"]):
            diffs.append(diff)
        if len(diffs) == 10:
            break
    if not diffs:
        raise BenchmarkError("对照结果里没有可用的差别。")
    lists = {key: [_text(item, 300) for item in (payload.get(key) if isinstance(payload.get(key), list) else [])[:8] if _text(item, 300)]
             for key in ("same", "caveats")}
    videos = [{"label": _text(item.get("label"), 20), "id": _text(item.get("id"), 25), "author": _text(item.get("author"), 100),
               "title": _text(item.get("title"), 200), "group": item.get("group") if item.get("group") in {"hit", "ctrl"} else ""}
              for item in (payload.get("videos") if isinstance(payload.get("videos"), list) else [])[:40] if isinstance(item, dict)]
    return {"hit": _count(payload.get("hit")), "ctrl": _count(payload.get("ctrl")), "diffs": diffs, **lists, "videos": videos}


class BenchmarkStore:
    def __init__(self, runtime_root):
        self.root = Path(runtime_root) / "benchmark"
        self.lock = threading.Lock()

    def _read(self, name, key):
        try:
            data = json.loads((self.root / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise BenchmarkError(f"对标记录读不出来，请检查运行目录 benchmark/{name}。", 500) from error
        return data.get(key, {}) if isinstance(data, dict) and isinstance(data.get(key), dict) else {}

    def _write(self, name, key, value):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, key: value}, handle, ensure_ascii=False, indent=1)
            os.replace(temp, self.root / name)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def save_contrast(self, product, payload, title=""):
        record = clean_contrast(payload)
        record.update(id="CON-" + uuid4().hex[:10], product=product, title=_text(title, 120), created_at=_now())
        with self.lock:
            contrasts = self._read("contrasts.json", "contrasts")
            contrasts[record["id"]] = record
            for old in sorted(contrasts, key=lambda key: contrasts[key].get("created_at", ""))[:-MAX_CONTRASTS]:
                del contrasts[old]
            self._write("contrasts.json", "contrasts", contrasts)
        return record

    def contrasts(self, product=None):
        with self.lock:
            contrasts = self._read("contrasts.json", "contrasts")
        wanted = product_key(product) if product else ""
        items = [dict(item, rules=rules_of(item)) for item in contrasts.values() if not wanted or product_key(item.get("product")) == wanted]
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def contrast(self, contrast_id):
        with self.lock:
            item = self._read("contrasts.json", "contrasts").get(contrast_id)
        if not item:
            raise BenchmarkError("这次对照拆解已不在工作台里，请在插件重新存一次。", 404)
        return dict(item, rules=rules_of(item))

    def check(self, task_id, round_id):
        with self.lock:
            return copy.deepcopy(self._read("checks.json", "checks").get(f"{task_id}:{round_id}"))

    def put_check(self, task_id, round_id, record):
        with self.lock:
            checks = self._read("checks.json", "checks")
            checks[f"{task_id}:{round_id}"] = record
            for old in sorted(checks, key=lambda key: checks[key].get("updated_at", ""))[:-MAX_CHECKS]:
                del checks[old]
            self._write("checks.json", "checks", checks)


def freeze(contrast, numbers, hook=None):
    """The part of a contrast (and an opening from the hook bank) a writing task keeps with it."""
    rules = contrast["rules"] if contrast else []
    if numbers is None:
        picked = rules
    else:
        if not isinstance(numbers, list) or any(type(n) is not int for n in numbers) or len(set(numbers)) != len(numbers):
            raise BenchmarkError("对标约束的勾选格式无效。")
        known = {rule["n"] for rule in rules}
        if set(numbers) - known:
            raise BenchmarkError("勾选的对标约束不在这次对照拆解里。", 409)
        picked = [rule for rule in rules if rule["n"] in numbers]
    frozen = {"rules": [{"n": index, "text": rule["text"], "basis": rule["basis"], "dim": rule["dim"]} for index, rule in enumerate(picked, 1)]}
    if contrast:
        frozen["source"] = {"id": contrast["id"], "title": contrast.get("title") or "", "created_at": contrast.get("created_at"),
                            "hit": contrast.get("hit"), "ctrl": contrast.get("ctrl")}
    if hook:
        frozen["hook"] = {"video_id": hook["video_id"], "author": hook.get("author") or "", "type": hook.get("type") or "",
                          "lines": copy.deepcopy(hook.get("lines") or [])[:3], "screen": hook.get("screen") or "", "why": hook.get("why") or ""}
        first = (hook.get("lines") or [{}])[0]
        frozen["rules"].append({"n": len(frozen["rules"]) + 1, "dim": "参考开头",
                                "text": f"开头用「{hook.get('type') or '参考'}」手法，参考 @{hook.get('author') or ''}：{first.get('en') or first.get('zh') or ''}",
                                "basis": "开头库"})
    if not frozen["rules"]:
        raise BenchmarkError("请至少勾选一条对标约束。")
    return frozen


def line_times(lines, market=None):
    """Start second of each spoken line at the market's planning reading speed."""
    wpm = markets.MARKETS.get(market or markets.DEFAULT, markets.MARKETS[markets.DEFAULT])["words_per_minute"]
    elapsed, out = 0.0, []
    for line in lines:
        out.append(round(elapsed))
        words = len(re.findall(r"[\w'’-]+", line.get("spoken") or "")) or 1
        elapsed += words * 60 / wpm
    return out


def check_prompt(benchmark, variants, market=None):
    rules = "\n".join(f"{rule['n']}. {rule['text']}（依据：{rule['basis'] or '用户指定'}）" for rule in benchmark["rules"])
    drafts = []
    for number, variant in enumerate(variants, 1):
        times = line_times(variant["lines"], market)
        rows = [f"第 {line['number']} 句 [约 {second} 秒] {line['spoken']}｜{line.get('zh') or ''}"
                + (f"｜画面：{line['proof']}" if line.get("proof") else "") for line, second in zip(variant["lines"], times)]
        drafts.append(f"【候选 {number}】{variant.get('label') or ''}\n" + "\n".join(rows))
    return (
        "你是带货短视频审稿助手，用简体中文回答。下面是用户写稿时勾选的对标约束，以及这一轮的候选稿（逐句口播｜中文｜画面要求，"
        "每句前标了按配音语速估算的开始秒数）。逐条判断每条候选稿有没有做到每一个约束。\n"
        "要求：只看给出的稿子；画面要求里没写的画面算没做到；时间按标注的估算秒数判断。做到的写出在第几句；"
        "没做到的写清差在哪，并给一句最小改法（说挪哪句、改哪句，不重写整条稿）。不要编造稿子里没有的内容。\n\n"
        f"〈对标约束〉\n{rules}\n\n〈候选稿〉\n" + "\n\n".join(drafts) +
        '\n\n只输出一个 JSON 对象，不要任何其他文字：{"variants":[{"variant":1,"items":[{"rule":1,"ok":true,"where":"第 1 句（约 0 秒）","fix":""}]}]}'
    )


def parse_check(output, variant_count, rule_count):
    found = re.search(r"\{.*\}", str(output or ""), re.S)
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    results = []
    for raw in data.get("variants") if isinstance(data, dict) and isinstance(data.get("variants"), list) else []:
        if not isinstance(raw, dict) or type(raw.get("variant")) is not int or not 1 <= raw["variant"] <= variant_count:
            continue
        items = {}
        for item in raw.get("items") if isinstance(raw.get("items"), list) else []:
            if isinstance(item, dict) and type(item.get("rule")) is int and 1 <= item["rule"] <= rule_count and isinstance(item.get("ok"), bool):
                items[item["rule"]] = {"rule": item["rule"], "ok": item["ok"], "where": _text(item.get("where"), 120), "fix": _text(item.get("fix"), 300)}
        results.append({"variant": raw["variant"], "items": [items[n] for n in sorted(items)]})
    return results or None


class BenchmarkWorkbenchMixin:
    """Workbench endpoints for 对标约束. Needs self.benchmark (BenchmarkStore), self.hook_bank, self.ai, self.products()."""

    def extension_save_contrast(self, body):
        if not isinstance(body, dict) or set(body) - {"product", "contrast", "title", "extension_version"}:
            raise BenchmarkError("对照结果格式错误。")
        product = body.get("product")
        if not isinstance(product, str) or not product.strip() or len(product) > 200:
            raise BenchmarkError("请选择产品。")
        if product.strip() not in self.products()["products"]:
            raise BenchmarkError("工作台里没有这个产品。", 404)
        record = self.benchmark.save_contrast(product.strip(), body.get("contrast"), body.get("title") or "")
        return {"ok": True, "id": record["id"], "rules": len(record["diffs"])}

    def benchmark_contrasts(self, product=None):
        return {"contrasts": self.benchmark.contrasts(product)}

    def benchmark_context(self, body, product):
        """creation_context.benchmark for a new writing task, or None when the request has none."""
        wanted = body.get("benchmark")
        if wanted is None:
            return None
        if not isinstance(wanted, dict) or set(wanted) - {"contrast_id", "rules", "hook_id"}:
            raise BenchmarkError("对标约束格式无效。")
        if body.get("action") not in (None, "variants"):
            raise BenchmarkError("按对标只用于写稿。")
        contrast = None
        if wanted.get("contrast_id"):
            contrast = self.benchmark.contrast(wanted["contrast_id"])
            if product_key(contrast["product"]) != product_key(product):
                raise BenchmarkError("这次对照拆解属于别的产品。", 409)
        hook = None
        if wanted.get("hook_id"):
            hook = next((item for item in self.hook_bank.list() if item["video_id"] == wanted["hook_id"]), None)
            if hook is None:
                raise BenchmarkError("开头库里已经没有这条开头。", 404)
        if not contrast and not hook:
            raise BenchmarkError("请选择一次对照拆解或一个参考开头。")
        return freeze(contrast, wanted.get("rules") if contrast else [], hook)

    def benchmark_check_status(self, task_id, round_id):
        record = self.benchmark.check(task_id, round_id)
        return record or {"status": "none"}

    def benchmark_check_start(self, body):
        """Start (once) the AI check of one completed round of a writing task that was written to a benchmark."""
        if not isinstance(body, dict) or set(body) - {"task_id", "round_id", "again"}:
            raise BenchmarkError("核对请求格式错误。")
        task_id, round_id = body.get("task_id"), body.get("round_id")
        task = self.ai.get(task_id)
        benchmark = (task.get("context") or {}).get("benchmark")
        if not benchmark:
            raise BenchmarkError("这个写稿任务没有按对标写。", 409)
        turn = next((item for item in task.get("rounds") or [] if item["round_id"] == round_id), None)
        if not turn or turn.get("status") != "completed" or not isinstance(turn.get("output"), str):
            raise BenchmarkError("这一轮还没有写完。", 409)
        from variant_parse import parse_variants
        variants = parse_variants(turn["output"])
        if not variants:
            raise BenchmarkError("这一轮没有认出候选稿。", 409)
        existing = self.benchmark.check(task_id, round_id)
        if existing and existing.get("status") in {"running", "done"} and not (body.get("again") and existing["status"] == "done"):
            return existing
        record = {"status": "running", "updated_at": _now(), "rules": benchmark["rules"], "results": []}
        self.benchmark.put_check(task_id, round_id, record)
        market = (task.get("context") or {}).get("market")

        def run():
            try:
                settings = self.ai.preferences(kind="background")
                output = self.ai.bridge.run(check_prompt(benchmark, variants, market), cwd=str(self.benchmark.root.parent),
                                            cancel_event=threading.Event(), on_progress=lambda message: None,
                                            model=settings.get("model"), reasoning_effort=settings.get("reasoning_effort"))
                results = parse_check(output, len(variants), len(benchmark["rules"]))
                final = ({**record, "status": "done", "results": results, "updated_at": _now()} if results
                         else {**record, "status": "failed", "error": "AI 没有按格式回答，可以再核对一次。", "updated_at": _now()})
            except Exception as error:  # A failed check only means no check is shown; the drafts are untouched.
                final = {**record, "status": "failed", "error": str(error)[:200] or "核对失败", "updated_at": _now()}
            self.benchmark.put_check(task_id, round_id, final)

        threading.Thread(target=run, daemon=True, name="benchmark-check").start()
        return record
