"use strict";

// UI sample data for designing pages without a real library. Off unless the address has ?demo=1 (full)
// or ?demo=long (long titles, many rows). Reads from the real server pass through; matched reads get sample
// data, and plan updates change the sample in memory only. Nothing is written to disk.
(() => {
  const mode = new URLSearchParams(location.search).get("demo");
  if (!mode) return;
  const long = mode === "long";
  const realFetch = window.fetch.bind(window);
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const today = new Date();
  const monday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - ((today.getDay() + 6) % 7));
  const day = (offset) => { const d = new Date(monday); d.setDate(d.getDate() + offset); return iso(d); };
  const product = "202H";
  const pad = (text, extra) => (long ? `${text}${extra}` : text);

  // ---- videos and weekly figures -----------------------------------------
  const accounts = ["sharpal.official", "knife.daily", "edge.lab"];
  const titles = ["磨刀石 30 秒救回钝刀", "露营刀开刃前后对比", "菜刀切番茄挑战", "钻石磨刀板开箱", "厨房刀具保养三步", "猎刀磨角度实测", "剪刀也能磨？", "陶瓷刀翻新"];
  const videos = [], weekly = [];
  for (let i = 0; i < (long ? 40 : 24); i++) {
    const posted = day(-7 * Math.floor(i / 3) + (i % 3) * 2 - (i < 3 ? 7 : 0));
    videos.push([`7${String(4100000000000 + i * 7919)}`, accounts[i % 3], product, posted]);
  }
  for (let w = 8; w >= 1; w--) {
    const week = day(-7 * w);
    videos.forEach(([, , , posted], index) => {
      if (posted > day(-7 * w + 6)) return;
      const age = Math.max(1, Math.round((new Date(week) - new Date(posted)) / 864e5 / 7) + 1);
      const views = Math.round((4000 + ((index * 3779) % 9000)) / age * (1 + (8 - w) * 0.08));
      const clicks = Math.round(views * 0.031), orders = Math.round(clicks * 0.09);
      weekly.push([index, week, views, Math.round(views * 1.6), clicks, orders, orders + (index % 2), +(orders * 23.9).toFixed(2)]);
    });
  }
  const summaryWeeks = [];
  for (let w = 8; w >= 1; w--) {
    const week = day(-7 * w), rows = weekly.filter((r) => r[1] === week);
    summaryWeeks.push({ week, videos: rows.length, views: rows.reduce((s, r) => s + r[2], 0), items: rows.reduce((s, r) => s + r[6], 0),
      gmv: rows.reduce((s, r) => s + r[7], 0), posted: videos.filter((v) => v[3] >= week && v[3] <= day(-7 * w + 6)).length, complete: w !== 5 });
  }
  const statuses = [["focus", "重点复制"], ["follow", "跟进"], ["watch", "观察"], ["pending", "待数据"], ["incomplete", "数据不全"], ["drop", "放弃"]];
  const videoList = videos.map(([video_id, account, prod, posted_on], i) => {
    const monday0 = new Date(posted_on); monday0.setDate(monday0.getDate() - ((monday0.getDay() + 6) % 7));
    const end = new Date(monday0); end.setDate(end.getDate() + 13);
    const window_end = iso(end), later = window_end > iso(today);
    const [status, status_label] = later ? statuses[3] : statuses[i % statuses.length];
    const rowsOf = weekly.filter((r) => r[0] === i), sum = (k) => rowsOf.reduce((total, r) => total + r[k], 0);
    const views = sum(2), clicks = sum(4), orders = sum(5), gmv = +sum(7).toFixed(2), windowViews = later ? null : Math.round(views * 0.6);
    return { video_id, account, product: prod, posted_on, posted_at: `${posted_on}T12:00:00`, window_end, status, status_label, views, impressions: sum(3), clicks, orders, items: sum(6), gmv,
      window_views: windowViews, ctr_pct: views ? +(clicks * 100 / views).toFixed(2) : null, ctor_pct: clicks ? +(orders * 100 / clicks).toFixed(2) : null, gpm: views ? +(gmv * 1000 / views).toFixed(2) : null,
      this_week_gmv: rowsOf.find((r) => r[1] === day(-7))?.[7] || 0, first_data: rowsOf[0]?.[1] || null, last_data: day(-1),
      review_comments: i === 1 ? 3 : i === 4 ? 12 : 0, review_open: i === 1 ? 1 : 0, case_id: i === 6 ? "PVR-000000000001" : null,
      headline: pad(titles[i % titles.length], "：这是一个特别长的视频标题，用来检查换行、截断和窄屏下的排版是否还能看清楚"),
      title: titles[i % titles.length] };
  });

  // ---- cases, tasks, plans ---------------------------------------------
  const caseTitles = ["磨刀石开箱 · 欧美厨房", "露营刀对比 · 户外博主", "番茄挑战 · 快节奏", "钝刀翻新 · 前后对比", "剪刀磨刃 · 意外用途", "刀具保养 · 教程向"];
  const cases = caseTitles.slice(0, long ? 6 : 5).map((title, i) => ({
    case_id: `PVR-0000000000${String(i + 1).padStart(2, "0")}`, product, archived: false, revision: "demo",
    title: pad(title, "，附带一段很长的案例名称说明用于测试布局"),
    updated_at: `${day(-i)}T10:00:00`, event_count: 1, search_text: title, assets: {}, sales_data: null, resolved_direction: null,
    current: {
      title: pad(title, "，附带一段很长的案例名称说明用于测试布局"), product, category: ["转化正向参考", "高播放低转化", "待验证与整理"][i % 3],
      kind: "performance", conclusion: ["开场 3 秒的失败画面留住了人，切纸对比是下单的转折点。", "播放很高但点击偏低，评论集中在价格，购买理由没有讲到。", "数据还不全，先保留开场和角度刻度两个可复用的点。"][i % 3],
      assessments: [{ dimension: "conversion", polarity: ["positive", "negative", "unknown"][i % 3], reason: ["CTOR 高于同期均值", "点击率低于 1%", "观察窗未结束"][i % 3] }],
      metrics: { video_id: videos[i][0], creator_handle: `@${videos[i][1]}`, published_title: titles[i % titles.length], views_display: ["48.6K", "126K", "9.8K", "31.2K", "15.4K", "7.1K"][i], product_clicks_display: ["1.28K", "980", "210", "860", "402", "160"][i], items_sold: [46, 12, 3, 28, 9, 2][i], ctor_display: ["3.6%", "1.2%", "1.4%", "3.3%", "2.2%", "1.3%"][i], ticket_value: "89.9" },
      evidence: { status: ["source_checked", "user_reported", "unverified"][i % 3], source: "后台导出", window: "发布当周 + 下一周", as_of: day(-1) },
      transcript: i % 3 === 0 ? undefined : { text: "My knife couldn't even cut a tomato.\nThirty seconds on this sharpener — and now it slices paper clean.\nThe angle is locked, so you can't mess it up.", status: "checked", source: "人工核对" },
      limitations: ["只有一个统计窗口，不能推断因果"], purchase_reason: "30 秒出锋 + 不用练手感", history_note: "",
      highlights: i % 2 ? ["开场钩子"] : [],
      test_direction: i % 4 === 1 ? undefined : { direction_id: ["D-锋利感", "D-省时", "D-耐用"][i % 3] },
      publish: i % 3 === 2 ? { published_on: day(-3) } : undefined,
    },
  }));
  const tasks = [
    { task_id: "AIT-000000000001", case_id: cases[0].case_id, case_title: pad("磨刀石开箱 · 生成 3 个变体", "（长标题测试）"), action: "variants", status: "running", product },
    { task_id: "AIT-000000000002", case_id: cases[1].case_id, case_title: "露营刀对比 · 一起审稿", action: "review", status: "queued", product },
    { task_id: "AIT-000000000003", case_id: cases[2].case_id, case_title: "番茄挑战 · 复盘", action: "retrospective", status: "completed", product },
    { task_id: "AIT-000000000004", case_id: cases[3].case_id, case_title: "钝刀翻新 · 变体", action: "variants", status: "completed", product },
  ];
  let revision = 1;
  const plans = [
    { id: "PLAN-1", product, direction_id: "D-锋利感", action: "scale", status: "pending", note: pad("复制 7 天内播放破万的开场，换 2 个新场景", "；另外补充一段很长的备注，用来检查多行备注的显示效果和按钮是否被挤压") },
    { id: "PLAN-2", product, direction_id: "D-省时", action: "execution", status: "working", note: "", task_id: "AIT-000000000002" },
    { id: "PLAN-3", product, direction_id: "D-耐用", action: "stop", status: "pending", note: "连续两轮低于 1000 播放" },
  ];

  // ---- reviews -----------------------------------------------------------
  const labels = {
    nature: { owner: "买家亲历", defect: "具体缺陷或限制", compare: "对比竞品", objection: "质疑或反对", question: "提问", advice: "使用建议", result: "结果与场景", buy_info: "问价格/入口/配送", praise: "泛好评", noise: "无信息" },
    topic: { clamp: "夹具与抓握", stability: "晃动、不稳、底座", angle: "角度刻度与精度", small_blade: "小刀与短薄刀", stones: "磨板耐用与品质", price: "价格与性价比", instructions: "说明书与上手", learning: "学习曲线与节奏", shipping: "配送、缺货与缺件", mirror: "镜面与抛光" },
    competitor: { worksharp: "Work Sharp", lansky: "Lansky", tormek: "Tormek" },
    role: { verified_buyer: "已验证买家", incentivized_buyer: "试用/有激励买家", unverified_buyer: "未验证购买者", viewer: "观众", peer: "社区讨论者", brand_account: "品牌或达人账号" },
    platform: { tiktok_video: "TikTok 视频评论", tiktok_shop: "TikTok Shop 评价", amazon: "亚马逊评价", reddit: "Reddit", facebook: "Facebook" },
  };
  const longText = "I've been sharpening knives for about ten years, mostly on whetstones, so I was skeptical going in. The clamp holds most of my kitchen knives well, but anything thinner than about 1.5mm tends to slip unless you tighten it a lot. The angle guide is accurate to within a degree or so when I checked with a digital gauge. The stones that come with it are fine for maintenance but the coarse one wore down noticeably after maybe fifteen knives. Overall it's a solid system for the price, and much faster than freehand once you get the setup routine down. ";
  const R = (id, platform, role, rating, posted, likes, text, nature, topic, extra = {}) => ({
    id: id.padEnd(16, "0"), platform, role, rating, posted_on: day(-posted), likes, title: extra.title || null, text, url: extra.url || null, context: extra.context || null,
    sku: extra.sku || null, is_reply: extra.is_reply ? 1 : 0, language: extra.language || "en", source_ref: platform === "tiktok_video" ? videos[posted % videos.length][0] : null,
    tags: { nature, topic, competitor: extra.competitor || [] }, replies: platform === "tiktok_video" && !extra.is_reply ? { total: extra.replies || 0, brand: extra.brand || 0 } : null,
    manual: {}, pick: null, first_seen: posted < 10 ? day(-posted) : day(-60), relevant: extra.irrelevant ? 0 : 1, parent_id: extra.parent || null,
  });
  const reviews = [
    R("a1", "amazon", "verified_buyer", 5, 3, 412, "Took my dull chef's knife to paper-slicing sharp in about 5 minutes. The angle guide makes it almost impossible to mess up.", ["owner", "result"], ["angle"], { title: "Finally a system that works", sku: "202H-Pro" }),
    R("a2", "amazon", "verified_buyer", 2, 12, 188, "The base wobbles on my granite counter no matter how I set it up. Had to put a rubber mat under it. For this price it should be stable out of the box.", ["owner", "defect"], ["stability", "price"], { title: "Wobbly base", sku: "202H" }),
    R("a3", "amazon", "incentivized_buyer", 4, 20, 64, longText + longText, ["owner", "compare", "advice"], ["clamp", "small_blade", "stones"], { title: "Good system with a few limits", competitor: ["worksharp"] }),
    R("v1", "tiktok_video", "viewer", null, 1, 2310, "Does this work on serrated knives? Asking for my bread knife 😅", ["question"], [], { replies: 0 }),
    R("v2", "tiktok_video", "viewer", null, 2, 1840, "That's a lot of wobble for a $90 sharpener", ["objection"], ["stability", "price"], { replies: 3, brand: 1 }),
    R("v3", "tiktok_video", "viewer", null, 4, 956, "How long does it take to learn? I always ruin my edges by hand", ["question"], ["learning"], { replies: 2 }),
    R("v4", "tiktok_video", "brand_account", null, 2, 120, "Great question! The 202H has a suction base option that fixes this on smooth counters.", ["advice"], ["stability"], { is_reply: true, parent: "v2".padEnd(16, "0") }),
    R("v5", "tiktok_video", "viewer", null, 6, 433, "Lansky has been doing this for 40 years for a third of the price", ["compare", "objection"], ["price"], { competitor: ["lansky"], replies: 0 }),
    R("s1", "tiktok_shop", "verified_buyer", 5, 5, 37, "Arrived fast, my husband sharpened every knife in the house the first night", ["owner", "result"], ["shipping"], { sku: "202H" }),
    R("s2", "tiktok_shop", "verified_buyer", 1, 9, 12, "Missing the 1000 grit stone in the box. Customer service hasn't replied in 4 days.", ["owner", "defect"], ["shipping"], { sku: "202H-Pro" }),
    R("s3", "tiktok_shop", "verified_buyer", 3, 15, 8, "Works but the instructions are useless, had to watch YouTube to figure out the clamp", ["owner", "defect"], ["instructions", "clamp"], { sku: "202H" }),
    R("r1", "reddit", "peer", null, 25, 89, "I compared it side by side with my Tormek for a week. For kitchen knives the edge is basically the same, for chisels the Tormek still wins.", ["compare", "result"], ["mirror"], { competitor: ["tormek"], context: "Guided sharpening systems under $150 — worth it?", url: "https://reddit.com/r/sharpening" }),
    R("r2", "reddit", "peer", null, 31, 41, "Anyone know if the replacement stones fit the older model?", ["question"], ["stones"], { context: "Sharpal 202H owners thread" }),
    R("f1", "facebook", "unverified_buyer", null, 8, 22, "Got one for my dad for Christmas, he hasn't stopped talking about it", ["praise"], [], { context: "Knife Sharpening Enthusiasts" }),
    R("f2", "facebook", "unverified_buyer", 4, 40, 15, "Good for my small pocket knives once I figured out how to clamp them near the tip", ["owner", "advice"], ["small_blade", "clamp"], { context: "EDC Knives" }),
    R("n1", "tiktok_video", "viewer", null, 3, 5, "first", ["noise"], [], { replies: 0, irrelevant: true }),
  ];
  if (long) for (let i = 0; i < 30; i++) reviews.push({ ...reviews[i % 15], id: `b${String(i).padStart(2, "0")}`.padEnd(16, "0"), likes: 30 - i });
  const visible = (r, f) => (f.include_brand || r.role !== "brand_account") && (f.include_irrelevant || r.relevant) && (f.include_empty || r.text);
  function reviewSearch(q) {
    const list = (k) => q.getAll(k), f = { include_brand: q.get("include_brand"), include_irrelevant: q.get("include_irrelevant"), include_empty: q.get("include_empty") };
    const words = (q.get("q") || "").toLowerCase().split(/\s+/).filter(Boolean), min = +(q.get("rating_min") || 0), max = +(q.get("rating_max") || 5);
    const base = reviews.filter((r) => visible(r, f) && (!words.length || words.every((w) => `${r.title || ""} ${r.text}`.toLowerCase().includes(w)))
      && (!q.get("role") || r.role === q.get("role")) && (!q.get("rating_min") && !q.get("rating_max") || r.rating != null && r.rating >= min && r.rating <= max)
      && (!q.get("since") || r.first_seen >= q.get("since")) && (!q.get("video") || r.source_ref === q.get("video")) && (!q.get("sku") || r.sku === q.get("sku"))
      && (!q.get("replied") || r.platform === "tiktok_video" && !r.is_reply && (q.get("replied") === "yes" ? r.replies?.total : !r.replies?.total))
      && ["nature", "topic", "competitor"].every((k) => !list(k).length || list(k).some((t) => t === "none" ? !r.tags[k].length : r.tags[k].includes(t))));
    const shown = base.filter((r) => !list("platform").length || list("platform").includes(r.platform));
    const count = (rows, key) => rows.reduce((m, r) => (m[key(r)] = (m[key(r)] || 0) + 1, m), {});
    const facets = { platform: count(base, (r) => r.platform), role: count(shown, (r) => r.role), rating: {} };
    for (const k of ["nature", "topic", "competitor"]) { facets[k] = {}; shown.forEach((r) => r.tags[k].forEach((t) => facets[k][t] = (facets[k][t] || 0) + 1)); const none = shown.filter((r) => !r.tags[k].length).length; if (none && k !== "competitor") facets[k].none = none; }
    const sorts = { likes: (a, b) => b.likes - a.likes, newest: (a, b) => b.posted_on.localeCompare(a.posted_on), oldest: (a, b) => a.posted_on.localeCompare(b.posted_on),
      rating_low: (a, b) => (a.rating ?? 9) - (b.rating ?? 9), rating_high: (a, b) => (b.rating ?? -1) - (a.rating ?? -1), longest: (a, b) => b.text.length - a.text.length };
    const sorted = [...shown].sort(sorts[q.get("sort")] || sorts.likes), limit = +(q.get("limit") || 50);
    const zh = { 晃动: ["wobble", "stable"], 价格: ["price"], 夹具: ["clamp"] }, expanded = (q.get("q") || "").split(/\s+/).filter((t) => zh[t]).map((t) => ({ term: t, english: zh[t] }));
    const vids = Object.fromEntries(videoList.map((v) => [v.video_id, { headline: v.headline, account: v.account, posted_on: v.posted_on, views: 48000, orders: 31, ctor_pct: 8.4, gmv: 742 }]));
    return { total: sorted.length, offset: 0, limit, reviews: sorted.slice(0, limit), facets, query: { expanded, unmapped: [] }, videos: vids };
  }
  const reviewSummary = () => ({
    total: reviews.length, default_visible: reviews.filter((r) => visible(r, {})).length,
    platforms: Object.entries(labels.platform).map(([platform, label]) => { const rows = reviews.filter((r) => r.platform === platform), rated = rows.filter((r) => r.rating != null);
      return { platform, label, total: rows.length, with_text: rows.length, brand: rows.filter((r) => r.role === "brand_account").length, irrelevant: rows.filter((r) => !r.relevant).length,
        avg_rating: rated.length ? +(rated.reduce((s, r) => s + r.rating, 0) / rated.length).toFixed(2) : null, first: day(-60), last: day(-1) }; }),
    tags: {}, roles: {}, imports: Array.from({ length: 7 }, (_, i) => ({ source_file: `export-${i}.csv` })), skus: ["202H", "202H-Pro"], products: ["202H"], synced_at: `${day(-1)}T09:30:00`,
    coverage: { tiktok_video: "只抓到点赞前 500 的评论，回复关系完整。", amazon: "美国站全部评价；Vine 评价标为试用买家。", reddit: "按关键词搜索，可能漏掉没写型号的帖子。" },
    videos: videos.slice(0, 6).map(([video_id], i) => ({ video_id, comments: 40 - i * 5, open_questions: 3 - (i % 3), last_comment: day(-i) })),
    new_recent: reviews.filter((r) => r.first_seen >= day(-14)).length, new_since: day(-14), new_days: 14, tag_edits: 0, picks: reviews.filter((r) => r.pick).length, state_error: null, labels,
  });

  // ---- writing: the AI service shown as connected, and full task records behind the list ----
  const answer = `## 变体 A · 问题-解决

🔴 钝刀 30 秒变锋利？现场切纸给你看 #磨刀 #厨房好物 #SHARPAL

**口播（配音用）**
My knife couldn't even cut a tomato. Thirty seconds on this sharpener — and now it slices paper clean. The angle is locked, so you can't mess it up.

**画面**
1. 钝刀压烂番茄特写（0–3 秒）
2. 夹刀、看刻度、来回 6 次（3–20 秒）
3. 切纸、切番茄对比（20–30 秒）

## 变体 B · 结果倒叙

🔴 这把刀 30 秒前还切不开番茄 #磨刀石 #开箱

**口播（配音用）**
This paper cut? Thirty seconds ago, this knife was useless. Here's the trick: a fixed angle that does the hard part for you.`;
  const roundDone = (id, instruction, output, extra = {}) => ({ round_id: id, status: "completed", instruction, output, created_at: `${day(-1)}T10:00:00`, completed_at: `${day(-1)}T10:02:10`,
    model: "claude-opus", requested_model: "claude-opus", reasoning_effort: "high", requested_reasoning_effort: "high", skill_selection: "auto", skills_status: "submitted",
    skills: [{ label: "产品视频脚本复盘", path: "skills/product-video-script-review/SKILL.md" }], review_context: { status: "attached", reviews: 14, synced_at: `${day(-1)}T09:30:00` },
    messages: [{ item_id: `${id}-c`, phase: "commentary", text: "读取方向链和 14 条用户评论摘要，按两种推进结构起草。", complete: true }, { item_id: `${id}-a`, phase: "final_answer", text: output, complete: true }],
    read_estimates: [{ label: "变体 A", seconds: 14, words: 35 }, { label: "变体 B", seconds: 10, words: 25 }], ...extra });
  const taskDetail = (summary) => {
    const base = { ...summary, product, instruction: "按 D-锋利感 写 2 个变体，30 秒内，开头 3 秒必须出现钝刀失败画面。", updated_at: `${day(0)}T09:12:00`, created_at: `${day(-1)}T10:00:00`,
      model: "claude-opus", context: { direction_id: "D-锋利感", materials: [{ title: "That's a lot of wobble for a $90 sharpener" }] }, base_revision: "demo", round_count: 1 };
    if (summary.status === "running") return { ...base, round_count: 2, progress: "正在起草第 2 轮…", rounds: [
      roundDone("r1", base.instruction, answer),
      { round_id: "r2", status: "running", instruction: "保留变体 A，把口播压到 20 秒，结尾加一句评论区互动。", created_at: `${day(0)}T09:10:00`, model: "claude-opus", reasoning_effort: "high", skill_selection: "auto", skills_status: "submitted",
        messages: [{ item_id: "r2-c", phase: "commentary", text: "保留变体 A 的推进，压缩口播。", complete: true }, { item_id: "r2-a", phase: "final_answer", text: "## 变体 A（20 秒版）\n\n🔴 钝刀 30 秒变锋利？现场切纸给你看 #磨刀\n\n**口播（配音用）**\nMy knife couldn't cut a tomato. Thirty seconds later — paper. Drop your dullest knife in the comments", complete: false }] }] };
    if (summary.status === "queued") return { ...base, progress: "排队中，前面还有 1 个任务", rounds: [{ round_id: "r1", status: "queued", instruction: base.instruction, created_at: `${day(0)}T09:11:00`, messages: [] }] };
    return { ...base, rounds: [roundDone("r1", base.instruction, answer)] };
  };

  tasks.forEach((t, i) => Object.assign(t, { updated_at: `${day(-i)}T09:${String(12 - i).padStart(2, "0")}:00`, model: "claude-opus", round_count: t.status === "running" ? 2 : 1,
    instruction: ["按 D-锋利感 写 2 个变体，30 秒内，开头 3 秒必须出现钝刀失败画面。", "帮我审一下这版口播：节奏是不是太慢？", "复盘：为什么播放 2 万但只出了 3 单？", "换成厨师朋友出镜，写一个身份认同版本。"][i] }));

  const demoScripts = [
    { id: "SCR-000000000001", product, direction_id: "D-锋利感", title: "钝刀 30 秒变锋利 · 问题-解决", version: 2, status: "published", video_id: videos[0][0], updated_at: `${day(-2)}T15:00:00`, text: "My knife couldn't even cut a tomato. Thirty seconds on this sharpener — and now it slices paper clean." },
    { id: "SCR-000000000002", product, direction_id: "D-锋利感", title: "30 秒前还切不开番茄 · 结果倒叙", version: 1, status: "adopted", updated_at: `${day(-1)}T11:00:00`, text: "This paper cut? Thirty seconds ago, this knife was useless. Here's the trick: a fixed angle." },
    { id: "SCR-000000000003", product, direction_id: "D-省时", title: "厨师朋友不信 · 身份认同", version: 1, status: "draft", updated_at: `${day(0)}T09:00:00`, text: "My chef friend said guided sharpeners are for beginners. So we raced." },
    { id: "SCR-000000000004", product, direction_id: null, title: "露营刀开刃前后", version: 3, status: "reviewed", updated_at: `${day(-6)}T10:00:00`, text: "Same knife, two weeks apart. One trip with a dull edge was enough." },
  ];
  const json = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.origin);
    const path = url.pathname, method = (init.method || "GET").toUpperCase();
    if (method === "GET") {
      if (path === "/api/performance/summary")
        return json({ coverage: { first: summaryWeeks[0].week, last: day(-1), gaps: [] }, weeks: summaryWeeks, imports: { files: 12, last: day(-1) }, totals_range: null, sources: [],
          rule: { thresholds: [[10000, "重点复制"], [3000, "跟进"], [1000, "观察"], [0, "放弃"]], window: "发布当周 + 下一周（周一至周日）的播放量" } });
      if (path === "/api/performance/analytics") return json({ videos, weekly, monthly: [], months: {}, shop_daily: [] });
      if (path === "/api/performance/videos") return json({ videos: videoList });
      if (path === "/api/ai/tasks") return json({ tasks });
      if (path === "/api/creation") return json({ plans: plans.filter((p) => ["pending", "working"].includes(p.status)), scripts: demoScripts, revision: `demo-${revision}` });
      const oneCase = path.match(/^\/api\/cases\/(PVR-\d{12})$/);
      if (oneCase) {
        const c = cases.find((x) => x.case_id === oneCase[1]);
        if (!c) return json({ error: "案例不存在" }, 404);
        const events = [
          { event_id: `${c.case_id}-E1`, created_at: `${day(-20)}T10:00:00`, kind: "original", text: "首次收录：从视频数据收为案例", current: c.current },
          { event_id: `${c.case_id}-E2`, created_at: `${day(-6)}T15:20:00`, kind: "fact", text: "补充一周后的数据和结论", current: c.current },
        ];
        return json({ ...c, original: { text: c.current.transcript?.text || "", source: "后台导出" }, events, current_event_id: events.at(-1).event_id, created_at: events[0].created_at,
          assets: {}, paths: { record: `cases/${c.case_id}`, reading: `library/${c.case_id}/case.md` } });
      }
      if (path === "/api/ai/status") return json({ available: true, label: "Claude 订阅已连接", providers: { claude: { available: true, label: "Claude 订阅已连接" } } });
      if (path === "/api/ai/options") { const real = await (await realFetch(input, init)).json(); return json({ ...real, models: (real.models || []).map((m) => ({ ...m, available: true })), skills: (real.skills || []).map((s) => ({ ...s, available: true })) }); }
      const aiTask = path.match(/^\/api\/ai\/tasks\/(AIT-[0-9a-f]{12})$/);
      if (aiTask) { const summary = tasks.find((t) => t.task_id === aiTask[1]); return summary ? json({ task: taskDetail(summary) }) : json({ error: "任务不存在" }, 404); }
      if (path === "/api/reviews/summary") return json(reviewSummary());
      if (path === "/api/reviews") return json(reviewSearch(url.searchParams));
      const one = path.match(/^\/api\/reviews\/([0-9a-z]{16})$/);
      if (one) { const r = reviews.find((x) => x.id === one[1]); const top = r?.parent_id || r?.id;
        return r ? json({ review: { ...r, thread: reviews.filter((x) => x.id !== r.id && (x.id === top || x.parent_id === top)) } }) : json({ error: "评论库里没有这条评论" }, 404); }
      if (path === "/api/reviews/tag-edits") return json({ edits: [] });
      if (path === "/api/state") {
        const response = await realFetch(input, init), data = await response.json();
        if (data.unchanged) return json(data);
        return json({ ...data, cases, signature: `demo-${data.signature}` });
      }
    }
    const reviewPost = path.match(/^\/api\/reviews\/([0-9a-z]{16})\/(pick|tag)$/);
    if (method === "POST" && reviewPost) {
      const body = JSON.parse(init.body || "{}"), r = reviews.find((x) => x.id === reviewPost[1]);
      if (reviewPost[2] === "pick") r.pick = body.picked ? { note: "", added: iso(today) } : null;
      else { const list = r.tags[body.kind]; r.tags[body.kind] = body.present ? [...new Set([...list, body.tag])] : list.filter((t) => t !== body.tag); r.manual = { ...r.manual, [body.kind]: { ...(r.manual[body.kind] || {}), [body.tag]: body.present } }; }
      return json({ review: r });
    }
    if (method === "POST" && path === "/api/reviews/sync") return json({ files: 7, reviews: reviews.length, added: 0, failed: [], missing_sources: [], state_error: null });
    if (method === "POST" && path === "/api/plans") {
      const body = JSON.parse(init.body || "{}"), plan = plans.find((p) => p.id === body.plan?.id);
      if (plan) Object.assign(plan, body.plan);
      revision++;
      return json({ ok: true, revision: `demo-${revision}` });
    }
    return realFetch(input, init);
  };
  document.documentElement.dataset.demo = mode;
})();
