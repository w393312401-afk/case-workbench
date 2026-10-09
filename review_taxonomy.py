"""Rule-based classification for the real-user review library.

Two independent layers, both visible as chips in the UI and searchable by the CLI:

* NATURE  - what kind of statement a review is (owner experience, defect, question...).
* TOPIC   - what it is about (clamp, angle scale, price, wrong listing option...).

The rules are plain regular expressions so they can be read, tested and tuned
without a model. Precision matters more than recall: a missed tag only hides a
review from one filter, a wrong tag misleads the person writing the script.

Two helpers for reading the library live here too: ZH_SEARCH teaches the search
box which English words a Chinese word stands for, and excerpt() cuts a long
review down to the sentence a rule matched.
"""
from __future__ import annotations

import re

NATURE = {
    "owner": "买家亲历",
    "defect": "具体缺陷或限制",
    "compare": "对比竞品",
    "objection": "质疑或反对",
    "question": "提问",
    "advice": "使用建议",
    "result": "结果与场景",
    "buy_info": "问价格/入口/配送",
    "praise": "泛好评",
    "noise": "无信息",
}

TOPIC = {
    "clamp": "夹具与抓握",
    "pivot": "翻转与旋转机构",
    "stability": "晃动、不稳、底座",
    "angle": "角度刻度与精度",
    "small_blade": "小刀与短薄刀",
    "edge_reach": "刃口末端磨不到",
    "blade_fit": "能磨哪些刀和工具",
    "stones": "磨板耐用与品质",
    "accessories": "替换磨板与配件",
    "instructions": "说明书与上手",
    "learning": "学习曲线与节奏",
    "price": "价格与性价比",
    "support": "客服与保修",
    "wrong_sku": "买错款式或购买页",
    "shipping": "配送、缺货与缺件",
    "build": "做工与品控",
    "case": "箱子与收纳",
    "mirror": "镜面与抛光",
    "repair": "修复、崩口与重塑",
    "many_knives": "一次磨很多把刀",
    "strop": "皮革荡刀",
    "handsharp": "手磨派与磨刀石派",
    "proof": "演示可信度",
    "ai_voice": "疑似 AI 声音或形象",
    "review_influence": "受评测视频影响",
    "gift": "送礼与做生意",
}

# Competing sharpeners and stones people name. A third kind of tag next to NATURE and TOPIC, so "who gets
# compared with this product, and how" can be counted. Brand names are Latin whatever language the comment is in.
# Only names that cannot be mistaken for something else: no "Arkansas" (a shipping question), no bare "Norton".
COMPETITOR = {
    "worksharp": "Work Sharp",
    "lansky": "Lansky",
    "tormek": "Tormek",
    "wicked_edge": "Wicked Edge",
    "kme": "KME",
    "tsprof": "TSPROF",
    "hapstone": "Hapstone",
    "xarilk": "Xarilk",
    "edge_pro": "Edge Pro",
    "horl": "Horl",
    "rolling": "滚刀器（Tumbler 等）",
    "shapton": "Shapton",
    "spyderco": "Spyderco Sharpmaker",
    "dmt": "DMT",
    "jende": "Jende",
    "anystone": "AnyStone",
}
COMPETITOR_RULES = {
    "worksharp": re.compile(r"work ?sharp|ken onion|precision adjust", re.IGNORECASE),
    "lansky": re.compile(r"lansky", re.IGNORECASE),
    "tormek": re.compile(r"tormek", re.IGNORECASE),
    "wicked_edge": re.compile(r"wicked edge", re.IGNORECASE),
    "kme": re.compile(r"\bkme\b", re.IGNORECASE),
    "tsprof": re.compile(r"tsprof", re.IGNORECASE),
    "hapstone": re.compile(r"hapstone", re.IGNORECASE),
    "xarilk": re.compile(r"xarilk|xarlik", re.IGNORECASE),
    "edge_pro": re.compile(r"edge pro\b", re.IGNORECASE),
    "horl": re.compile(r"\bhorl\b", re.IGNORECASE),
    "rolling": re.compile(r"\btumbler\b|rolling sharpener", re.IGNORECASE),
    "shapton": re.compile(r"shapton", re.IGNORECASE),
    "spyderco": re.compile(r"spyderco|sharpmaker", re.IGNORECASE),
    "dmt": re.compile(r"\bdmt\b", re.IGNORECASE),
    "jende": re.compile(r"jende", re.IGNORECASE),
    "anystone": re.compile(r"anystone", re.IGNORECASE),
}


def competitors(text):
    """The competitor codes a review names."""
    return {code for code, rule in COMPETITOR_RULES.items() if rule.search(text or "")}


# Brand and creator accounts on TikTok / Facebook. Their own replies are kept but
# labelled, and hidden from the default view: they are not the audience's voice.
BRAND_ACCOUNTS = {"bushcraftedge", "bladesharptrevor", "sharpalusa", "sharpal_usa", "sharpal_us", "sharpaledgelab", "zackontheedge"}
BRAND_PROFILE = re.compile(r"sharpal", re.I)

ROLE_LABELS = {
    "verified_buyer": "已验证买家",
    "incentivized_buyer": "试用/有激励买家",
    "unverified_buyer": "未验证购买者",
    "viewer": "观众",
    "peer": "社区讨论者",
    "brand_account": "品牌或达人账号",
}

PLATFORM_LABELS = {
    "tiktok_video": "TikTok 视频评论",
    "tiktok_shop": "TikTok Shop 评价",
    "amazon": "亚马逊评价",
    "reddit": "Reddit",
    "facebook": "Facebook",
}

_I = re.IGNORECASE


def _r(pattern):
    return re.compile(pattern, _I)


TOPIC_RULES = {
    "clamp": _r(r"\bclamps?\b|\bjaws?\b|holder (doesn|does not|isn|not)|knife holder|tightening screws?|strip the screws|grip(s|ped)? (the )?(blade|knife)|(hold|holds|holding) (the )?(blade|knife)|center clamp|single clamp"),
    "pivot": _r(r"\bpivot|rotat(e|ion|ing) (mechanism|assembly)|rotating assembly|flip(ping)? (mechanism|system)|\bflip\b.{0,30}(stuck|froze|smooth|cumbersome)|swivel|sieze|seiz(ed|e)|froze\b|\bflipping\b"),
    "stability": _r(r"wobbl|\bflex(es|ed|ing)?\b|deflect|rocking|\brocks\b|(base|unit) (tips|is not flat|doesn.t lay flat)|base was more heavy|fixture is moving|\bdrift"),
    "angle": _r(r"angle (finder|gauge|block|cube|scale|dial)|digital (angle|level)|degree marks?|degree of angle|angles? (is|are) (not )?(accurate|inaccurate|off|different)|accuracy of angle|not accurate|angle.{0,25}(trial and error|a bit of a guess)|dials? in the back|set the angle|which angle|what angle|angle for each blade"),
    "small_blade": _r(r"\bsmall(er)?\b.{0,25}(knife|knives|blade)|pocket ?knives?|leatherman|multi.?tool|short(er)? (and )?(thin|blade)|(thin|short|narrow)(ner)? blades?|folder\b|3.? ?(in|inch|\")\b.{0,10}blade|fillet|paring"),
    "edge_reach": _r(r"(fall|falls) short of the edge|all the way to the edge|last bit of the knife|little edge on the stones|protrude|stone scrapes the holder|(heel|tip) (of the )?(knife|blade)"),
    "blade_fit": _r(r"\bswords?\b|katana|machete|cleaver|serrat|\baxes?\b|hatchet|chisel|scissor|clipper|recurve|curved blades?|concave|tanto|steak knives|large kitchen knives|chopping knives|longest blade"),
    "stones": _r(r"(stones?|plates?|abrasives?|diamond|ceramic|whetstones?|wet stones?|3000|6000).{0,60}(soft|wear|worn|deteriorat|poor|lacking|weak|bad|terrible|don.t last|lose|lost|cutting power|dish|replace)|(poor|bad|terrible|soft)\s+(quality\s+)?(stones?|ceramic|plates?)|stones are very|3000 and 6000"),
    "accessories": _r(r"replacement|refill|replace the (grits|stones)|additional stones|extra stones|change the stones|universal (stone )?holder|aftermarket|hapstone (stone )?holder|1x6|1 ?[x×] ?6"),
    "instructions": _r(r"instructions?|manual|quick.?start|directions|assembly|assemble"),
    "learning": _r(r"learning curve|takes? (a )?(bit|few minutes|some time|time)|first (knife|time|setup)|get (the )?hang|finicky|not fool ?proof|trial and error|practice"),
    "price": _r(r"\$\s?\d|\bprice|pricey|expensive|overpriced|over priced|cheaper|worth (it|the|every)|value|bang for|for less|not worth|how much|precio|preis|costo|bucks|steal\b|reasonable|affordable|too much money"),
    "support": _r(r"customer (service|support)|warranty|sent me (a )?replacement|they sent|refund|return(ed)?\b|support got back"),
    "wrong_sku": _r(r"bait and switch|all (i|I) got was|only receiving a leather strop|misleading|trickery|\bstyle\b.{0,20}option|advertised (as|at) the whole|whole (kit|system|thing).{0,40}(leather strop|part)"),
    "shipping": _r(r"alaska|colorado|deliver|\bshipp?(ing|ed)?\b|haven.?t received|missing (one )?part|arrived (on time|broken|damaged)|delay|fast delivery|available to"),
    "build": _r(r"well[- ]made|build quality|high quality|built like a tank|sturdy|solid (case|metal|parts)|heavy duty|made of|metal (body|construction|build)|plastic|aluminum|defect|dud\b|quality control|\bqc\b|allen key|damaged|missing"),
    "case": _r(r"\bcase\b|carry(ing)?\b|portable|organi[sz]"),
    "mirror": _r(r"mirror|polish|reflect|shiny|\bshine\b|toothy"),
    "repair": _r(r"\bchips?\b|chipped|reshape|reprofile|re-profile|reset(ting)? (the )?bevel|brought .{0,25}back to life|put an edge back|severely dull|abused|nicked|damaged edge|factory (fault|edge)"),
    "many_knives": _r(r"(\b\d{1,3}|many|several|all of my|all my|every|entire).{0,25}knives|butcher block|first night|knife block|whole (block|drawer)"),
    "strop": _r(r"\bstrop\b|leather strop|stropping|green (polishing )?compound|polishing compound"),
    "handsharp": _r(r"skill issue|learn(ed)? (to|how)|by hand|hand[- ]?sharpen|free ?hand|whet ?stones?|wet ?stones?|water ?stones?|samurai|real knife guys|gadget|oilstone|walmart smiths?"),
    "proof": _r(r"phony|show me|tomato|shave my arm|paper means sharp|\$3 knife|cut paper|slice paper|easy.slice"),
    "ai_voice": _r(r"\bai\b|owen wilson|white person|clear its throat|robot|voice.?over"),
    "review_influence": _r(r"youtube|outdoors?55|neeves|(saw|watch(ed|ing)).{0,25}review|review (on|by)\b|after (seeing|watching)"),
    "gift": _r(r"\bgift|husband|wife|\bdad\b|father|birthday|sell knives|sharpening services|offer knife sharpening|bring(ing)? their knif"),
}

COMPARE = _r(r"work ?sharp|precision adjust|lansky|tormek|kme\b|wicked edge|tsprof|hapstone|xarilk|xarlik|edge pro|ken onion|horl|tumbler|shapton|spyderco|sharpmaker|dmt\b|jende|rolling sharpener|anystone|cabela|amazon.{0,20}(cheaper|less)|big 5|norton|arkansas")
OWNER = _r(r"\bi (just )?(bought|purchased|ordered|received|got (mine|one|this|it))\b|\bi have (the |a |one |this )?(sharpal|202)|\bmy 202|got (mine|it) (today|yesterday)|just got (it|one|mine)|love mine|\bmine (is|works|has|seized|arrived)\b|i('ve| have) (sharpened|used|put|been using)|i (sharpened|used)\b|(i |we )(did|reshaped|brought)|took me|i own")
DEFECT = _r(r"stuck|seiz|froze|flex|wobbl|doesn.t (hold|securely|work well|stay)|does not (hold|stay)|would not stay|not (accurate|flat|great|good|enough|optimized)|isn.t (perfect|great|accurate)|fall short|too (small|short|thin|stiff|large)|difficult|cumbersome|hard to|issue|problem|complain|poor|soft\b|lacking|missing|broken|defect|dud\b|strip|scrapes|limited|can.t (grip|hold|reach)|cannot (grip|hold|reach|be sharpened)|unusable|disappoint|wish (the|it|they|i)|only (real )?(complaint|gripe|problem)|downside|deflection|quality control|not (a )?(fan|work)")
OBJECTION = _r(r"skill issue|learn to|just learn|not worth|overpriced|too expensive|pricey|gimmick|scam|useless|pointless|slippery slope|waste|lightweights?|cheaper|for less|\bphony\b|bs!|not for \d+|don.t waste|drift isn.t|doesn.t need|you don.t need|can.t beat|better than this|great setup for a lot less|failed then|hand sharpened|as good or better|chasing the mirror|stickier|not last as long|lessen")
ADVICE = _r(r"make sure|readjust|re-?set the angle|you should|you need to|tip\b|hint\b|be careful|remember to|don.t forget|i suggest|i.d suggest|just look up|use (a|the|one) (marker|sharpie|angle|clamp)|painter.?s tape|stop at \d+|start (with|at)|then \d+, ?\d+|3 ?, ?4 ?, ?6|after 1k|clamp(ing)? (on|the)|section by section|only using one")
RESULT = _r(r"razor|shave|hair[- ]whittling|scary sharp|super sharp|very sharp|sharpest|so sharp|(got|get|have|put|putting|achieved?|achieving|with a) (a |the |an? )?mirror (finish|shine|edge|polish)|sliced (a )?thin|cut(s)? (through )?paper|like new|back to life|\d+ knives|first night|brought .{0,25}(back|to life)|best edge|great edge|amazing edge|it.s like a razor|shave with|reshape|hair whittl|glides through")
BUY_INFO = _r(r"how much|what is the price|price\s*\??\s*$|price (plz|please)|where (can|do|to) (i |we )?(buy|get|find|order)|where to buy|how (do|can) i (buy|get|order)|\bpreis\s*\?|precio|costo\b|cu[aá]nto|d[oó]nde|comprar|adquirir|link\s*\?|ship(ping)? (to|available)|deliver(able|y)? to|available to|alaska|colorado|تكلف|بكام|السعر|fiyat|价|订购|買|订|gi[aá]\b")
PRAISE = _r(r"\b(great|good|awesome|amazing|excellent|love|loved|best|wonderful|fantastic|perfect|nice|super|w[oa]w)\b")
QUESTION_START = _r(r"^\s*(can|could|will|would|does|do|is|are|has|have|did|how|what|where|why|which)\b")
PRODUCT_WORDS = _r(r"sharpener|product|system|kit|tool|thing|set\b|202|sharpal|unit|machine|jig")
STOPWORDS = {"the", "a", "an", "and", "is", "it", "to", "of", "for", "this", "that", "with", "i", "you", "my", "on", "in", "are", "was", "but", "have", "not", "be", "so", "very", "can", "if", "all", "me", "just", "at", "as", "from", "or", "your", "they", "these", "your", "about", "what", "how", "get", "got", "one", "no", "do", "does", "will"}
NON_LATIN = re.compile(r"[Ѐ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿぀-ヿ㐀-鿿가-힯]")
FOREIGN_HINT = _r(r"\b(el|la|los|las|que|como|donde|para|por|es|muy|bom|obrigado|como|onde|quanto|der|die|das|und|ist|nicht|sehr|preis|est|pas|tr[eè]s|merci|une|dans|costo|cuanto|bu|bir|ve|fiyat|abi|il|di|che|molto|grazie)\b")
LETTER = re.compile(r"[A-Za-zÀ-ÿЀ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿぀-ヿ㐀-鿿가-힯]")


def detect_language(text):
    """'en' when the text reads as English, 'none' when it has no letters, else 'other'."""
    if not LETTER.search(text or ""):
        return "none"
    if NON_LATIN.search(text):
        return "other"
    tokens = re.findall(r"[A-Za-zÀ-ÿ']+", text.lower())
    if not tokens:
        return "none"
    hits = sum(1 for t in tokens if t in STOPWORDS)
    foreign = len(FOREIGN_HINT.findall(text))
    accents = len(re.findall(r"[À-ÿ]", text))
    if foreign >= 2 or (foreign and hits == 0) or accents >= 2 and hits < len(tokens) * 0.2:
        return "other"
    if len(tokens) >= 4 and hits / len(tokens) < 0.08:
        return "other"
    return "en"


# ---- Chinese search words -> English search terms ------------------------------------------
# The reviews are English. A Chinese word typed into the search box is also searched as the English words
# people use for it (the page lists what it expanded to). One row per idea: (Chinese words, English terms).
# English terms are matched as plain substrings, case-insensitive, so "rotat" finds rotate / rotation. Keep
# them specific: a fragment that sits inside common words ("thin" in "something", "bs" in "absolutely") would
# make every search noisy. Add a row here to teach the search a new word; no other change is needed.
ZH_SEARCH = (
    (("夹具", "夹子", "夹持", "夹紧", "固定器"), ("clamp", "jaw", "holder", "vise")),
    (("翻转", "旋转", "转轴", "转动"), ("pivot", "flip", "rotat", "swivel")),
    (("晃动", "摇晃", "松动", "不稳", "晃"), ("wobbl", "flex", "loose", "deflect", "rocking", "drift")),
    (("底座", "底盘"), ("the base", "base is", "base of", "slip")),
    (("角度", "刻度", "角度尺"), ("angle", "degree", "dial", "gauge")),
    (("小刀", "短刀", "折刀", "水果刀", "口袋刀"), ("pocket", "small", "short", "narrow", "paring", "folder", "thin blade", "thin knife")),
    (("磨板", "磨石", "磨刀石", "砂板", "钻石板", "陶瓷板"), ("stone", "plate", "whetstone", "diamond", "ceramic", "grit", "abrasive")),
    (("目数", "粒度"), ("grit",)),
    (("配件", "补充装", "替换", "备件"), ("replacement", "accessor", "refill", "spare", "additional")),
    (("说明书", "使用说明", "说明", "教程", "指南"), ("instruction", "manual", "direction", "tutorial", "guide")),
    (("上手", "新手", "入门", "学习曲线", "难度"), ("learning curve", "beginner", "practice", "hang of", "difficult", "trial and error")),
    (("价格", "价钱", "价位", "多少钱", "性价比", "值不值", "贵", "便宜"), ("price", "expensive", "cheap", "cost", "worth", "value", "how much", "pricey", "bucks", "$")),
    (("保修", "质保", "售后", "客服"), ("warranty", "guarantee", "refund", "customer service", "support")),
    (("买错", "错款", "买错款", "发错"), ("wrong", "bait", "misleading", "advertised", "all i got")),
    (("发货", "物流", "配送", "快递", "缺货", "缺件", "到货"), ("ship", "deliver", "arrive", "missing", "delay", "alaska")),
    (("做工", "质量", "品控", "材质", "金属", "塑料", "结实"), ("quality", "build", "sturdy", "metal", "plastic", "solid", "flimsy", "defect")),
    (("收纳", "箱子", "收纳箱", "便携", "携带"), ("case", "carry", "storage", "organiz", "portable")),
    (("镜面", "抛光", "光亮"), ("mirror", "polish", "shine")),
    (("荡刀", "皮革", "皮带", "磨刀带"), ("strop", "leather", "compound")),
    (("抛光膏", "研磨膏"), ("compound", "polishing")),
    (("修复", "崩口", "缺口", "重塑", "修刃"), ("repair", "chip", "reshape", "reprofile", "restore", "damaged")),
    (("开刃", "刃角", "斜面"), ("bevel",)),
    (("钝刀", "钝"), ("dull", "blunt")),
    (("手磨", "徒手", "手工磨"), ("by hand", "freehand", "free hand", "hand sharpen", "skill issue")),
    (("质疑", "怀疑", "造假", "假的", "噱头", "智商税"), ("phony", "fake", "scam", "gimmick", "show me", "staged")),
    (("配音", "人声", "数字人", "机器人"), ("owen wilson", "robot", "white person", "clear its throat", "voiceover", "voice over")),
    (("送礼", "礼物", "老公", "爸爸", "父亲节"), ("gift", "husband", "dad", "wife", "father")),
    (("斧头", "斧"), ("axe", "hatchet")),
    (("武士刀", "日本刀", "砍刀", "剑"), ("sword", "katana", "machete")),
    (("锯齿刀", "锯齿", "锯刃"), ("serrat", "seriat")),
    (("剪刀",), ("scissor", "clipper", "shears")),
    (("凿子", "刨刀"), ("chisel", "plane iron")),
    (("弯刀", "弧形", "弧度"), ("curved", "recurve", "concave", "tanto")),
    (("菜刀", "厨刀", "厨师刀", "切肉刀", "牛排刀"), ("chef", "kitchen", "cleaver", "butcher", "steak", "santoku")),
    (("竞品", "对手"), ("work sharp", "worksharp", "lansky", "tormek", "wicked edge", "kme", "edge pro", "hapstone", "xarilk", "ken onion", "spyderco", "shapton")),
    (("产地", "美国制造", "国产", "中国制造"), ("made in", "usa")),
    (("安装", "组装", "装配", "调试"), ("assembl", "setup", "set up", "install", "screw")),
    (("螺丝", "内六角", "扳手"), ("screw", "allen")),
    (("锋利", "剃须", "剃毛", "削纸", "切纸", "番茄"), ("razor", "shav", "hair", "paper", "tomato")),
    (("测评", "评测", "视频", "油管"), ("review", "youtube", "video", "outdoor55")),
    (("好评", "喜欢", "满意", "推荐", "好用"), ("love", "great", "awesome", "amazing", "excellent", "recommend", "happy", "satisf")),
    (("差评", "失望", "不好", "垃圾"), ("disappoint", "poor", "waste", "junk", "bad", "worst")),
    (("缺点", "缺陷", "问题", "毛病", "投诉", "不足"), ("issue", "problem", "defect", "complain", "downside", "flaw", "disappoint")),
    (("重量", "重", "沉"), ("heavy", "weight")),
    (("马克笔", "记号笔", "蓝墨水"), ("marker", "sharpie", "blue ink")),
    (("户外", "野外", "狩猎", "露营", "钓鱼"), ("hunting", "outdoor", "camp", "bushcraft", "fishing")),
    (("多功能刀", "多功能工具", "军刀"), ("multitool", "multi-tool", "leatherman")),
    (("磨损", "损耗", "耐用"), ("wear", "worn", "wore", "durab")),
    (("步骤", "流程"), ("step",)),
    (("一整套", "套装", "整套"), ("kit", "complete", "full set")),
    (("多久", "时间", "快", "慢"), ("minute", "hour", "quick", "fast", "slow")),
    (("磨刀", "磨"), ("sharpen", "hone")),
    (("学会", "学习"), ("learn",)),
)
# Longest word first, so "抛光膏" is taken as one word before "抛光" can claim part of it. A single character
# only counts when it is the whole search word: "重" alone is "heavy", but "重要" must not turn into "heavy".
_ZH_WORDS = sorted(((word, terms) for words, terms in ZH_SEARCH for word in words), key=lambda item: -len(item[0]))


def zh_groups(token):
    """The Chinese words found in a search token, in reading order, each with its English search terms."""
    taken, found = [False] * len(token), []
    for word, terms in _ZH_WORDS:
        if len(word) == 1 and token != word:
            continue
        at = token.find(word)
        while at >= 0:
            if not any(taken[at:at + len(word)]):
                taken[at:at + len(word)] = [True] * len(word)
                found.append((at, word, terms))
            at = token.find(word, at + 1)
    seen, groups = set(), []
    for _, word, terms in sorted(found):
        if terms not in seen:  # two words for one idea in the same token ask for it once
            seen.add(terms)
            groups.append((word, terms))
    return groups


_SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def excerpt(text, *patterns, limit=240):
    """The part of a long review that says what a rule looked for: its sentence (plus the next ones while they
    fit), cut to `limit` characters. A short text comes back whole; with no match the opening is used."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    sentences = [s for s in _SENTENCE_BREAK.split(text) if s]
    start = next((i for i, s in enumerate(sentences) if any(p.search(s) for p in patterns)), 0)
    chosen, end = sentences[start], start + 1
    if len(chosen) > limit:
        hit = min((m.start() for p in patterns for m in [p.search(chosen)] if m), default=0)
        begin = max(0, hit - limit // 3)
        chosen = ("…" if begin else "") + chosen[begin:begin + limit].strip()
        return chosen + ("…" if begin + limit < len(sentences[start]) else "")
    while end < len(sentences) and len(chosen) + 1 + len(sentences[end]) <= limit:
        chosen += " " + sentences[end]
        end += 1
    return ("…" if start else "") + chosen + ("…" if end < len(sentences) else "")


def classify(text, *, title="", rating=None, role="viewer", is_reply=False):
    """Return (nature_tags, topic_tags, language) for one review."""
    body = (text or "").strip()
    whole = f"{title or ''} {body}".strip()
    language = detect_language(whole)
    nature, topics = set(), set()
    buyer = role in {"verified_buyer", "incentivized_buyer", "unverified_buyer"}
    # The rules are written for English; other languages keep only the rules that are script-based.
    if language != "other":
        for code, rule in TOPIC_RULES.items():
            if code == "handsharp" and buyer:
                continue
            if rule.search(whole):
                topics.add(code)
    if buyer and body:
        nature.add("owner")
    elif language != "other" and OWNER.search(whole):
        nature.add("owner")
    if language != "other":
        if COMPARE.search(whole):
            nature.add("compare")
        if DEFECT.search(whole) and not ("handsharp" in topics and not buyer):
            nature.add("defect")
        if OBJECTION.search(whole) and not (buyer and (rating or 0) >= 4):
            nature.add("objection")
        if ADVICE.search(whole):
            nature.add("advice")
        if RESULT.search(whole):
            nature.add("result")
    # A question is a short comment with a question mark, or one that opens like a question.
    # Buyers' long reviews are not questions even when they contain a rhetorical "?".
    question_form = ("?" in body and len(body) <= 400 and not (buyer and len(body) > 160)) or (len(body) <= 140 and QUESTION_START.search(body) and "?" in body or len(body) <= 100 and QUESTION_START.search(body))
    if question_form and not buyer:
        nature.add("question")
    elif question_form and buyer and body.rstrip().endswith("?") and len(body) <= 160:
        nature.add("question")
    # Asking about price, where to buy or shipping: only non-buyers ask; buyers saying "worth the price" are not asking.
    if not buyer and BUY_INFO.search(whole):
        nature.add("buy_info")
    letters = len(re.findall(r"[A-Za-zÀ-ÿЀ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿぀-ヿ㐀-鿿가-힯]{2,}", body))
    informative = nature - {"owner"}
    if not body:
        nature = set() if rating is not None else {"noise"}
    elif language == "none" or (letters < 2 and not (informative or topics)):
        nature.add("noise")
    elif not informative and not topics and language != "other":
        if rating is not None and rating >= 4 and len(body) <= 200 or len(body) <= 140 and PRAISE.search(whole) and (buyer or PRODUCT_WORDS.search(whole)):
            nature.add("praise")
        elif letters < 3 and not buyer:
            nature.add("noise")
    return nature, topics, language
