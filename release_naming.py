"""成品统一命名：传 Notion 的页面名和导出文件名，一个规则。

页面名（2026-11-24 fly 定）：「国旗 形式 产品-达人-日期-序号版本」，如 🇺🇸 ▶️ 202-Zack-1124-1、🇩🇪 🧩 198-Zack-1124-2B。
▶️ 是视频，🧩 是图文；版本只有图文有（A、B、C），外语版 A-DE 的版本仍是 A，国家看市场。
文件名不放表情符号，国旗换成国家代码放最前：US-202-Zack-1124-1、DE-198-Zack-1124-2B。

所有字段都从达芬奇时间线名（达人 + 年月日 + -编号- + 序号，如 Zack261124-198-2）拆出来，不另存，
旧时间线和已上传的记录不用改。手动改过、拆不开的时间线名原样放在国旗和形式后面。
"""
from __future__ import annotations

import re

import markets

FLAGS = {"us": "🇺🇸", "uk": "🇬🇧", "de": "🇩🇪", "es": "🇪🇸"}
CODES = {"us": "US", "uk": "UK", "de": "DE", "es": "ES"}
FORMS = {"video": "▶️", "photo": "🧩"}
_TIMELINE = re.compile(r"^(?P<creator>.+?)(?P<date>\d{6})-(?P<code>[A-Za-z0-9]+)-(?P<seq>\d+)(?P<tail>-\S.*)?$")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def parse_timeline(name, creator=""):
    """{creator, mmdd, code, seq, tail} from 「Zack261124-198-2」 or 「Zack261105-202-1-家里」; None when the name was set by hand."""
    match = _TIMELINE.match(str(name or "").strip())
    if not match or (creator and match["creator"].casefold() != str(creator).strip().casefold()):
        return None
    return {"creator": match["creator"], "mmdd": match["date"][2:], "code": match["code"], "seq": match["seq"], "tail": match["tail"] or ""}


def _version(version):
    """A-DE → A: the market already says which language it is."""
    return str(version or "").split("-", 1)[0].strip()


def core(timeline_name, version="", creator=""):
    parts = parse_timeline(timeline_name, creator)
    if parts:
        return f"{parts['code']}-{parts['creator']}-{parts['mmdd']}-{parts['seq']}{_version(version)}{parts['tail']}"
    return " ".join(part for part in (" ".join(str(timeline_name or "").split()), _version(version)) if part)


def release_name(timeline_name, market="", form="video", version="", creator=""):
    """Notion 页面名：🇩🇪 🧩 198-Zack-1124-2B。"""
    market = markets.stored(market)
    return " ".join(part for part in (FLAGS.get(market, ""), FORMS[form], core(timeline_name, version, creator)) if part)


def release_filename(timeline_name, market="", version="", creator=""):
    """文件和文件夹名：DE-198-Zack-1124-2B，Windows 不收的字符换成 _。"""
    market = markets.stored(market)
    return _UNSAFE.sub("_", f"{CODES.get(market, market.upper())}-{core(timeline_name, version, creator)}".replace(" ", "-"))
