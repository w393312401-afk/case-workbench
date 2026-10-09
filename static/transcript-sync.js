"use strict";

// 视频播放时，让带时间戳的完整文案逐句高亮并滚动到可视位置。
// 只处理 "[0.00s - 3.60s] 文字" 这种逐行带时间段的文案；没有时间戳的文案保持原样。
(() => {
  const LINE = /^\s*\[([^\]]+)\]\s*(\S[\s\S]*)$/;
  const CLOCK = /^\d+(?::\d{1,2}){0,2}(?:[.,]\d+)?s?$/;
  const MANUAL_SCROLL_PAUSE_MS = 4000;

  function seconds(part) {
    part = part.trim();
    if (!CLOCK.test(part)) return null;
    return part
      .replace(/s$/, "")
      .replace(",", ".")
      .split(":")
      .reduce((total, unit) => total * 60 + Number(unit), 0);
  }

  function parseTimedLine(line) {
    const match = LINE.exec(line);
    if (!match) return null;
    const range = match[1].split(/\s*[-–—]\s*/).map(seconds);
    if (range.length !== 2 || range.some((v) => v === null) || range[1] < range[0])
      return null;
    return { start: range[0], end: range[1] };
  }

  // 所有非空行都带时间段才算可同步；混合或无时间戳一律不同步。
  function parseCues(text) {
    const lines = String(text || "").split(/\r?\n/).filter((line) => line.trim());
    const cues = lines.map((line) => {
      const time = parseTimedLine(line);
      return time && { ...time, text: line.trim() };
    });
    return cues.length && cues.every(Boolean) ? cues : [];
  }

  // 当前句 = 开始时间不晚于播放位置的最后一句；两句之间的空档继续停在上一句。
  function activeIndex(cues, time) {
    let index = -1;
    cues.forEach((cue, i) => {
      if (cue.start <= time + 0.05) index = i;
    });
    return index;
  }

  // One controller owns segment playback, so transcript and native-expression buttons
  // cannot leave competing timers behind when users change the selected segment.
  function createRangeController(video, changed = () => {}) {
    let range = null;
    const clear = () => { range = null; changed(); };
    const play = () => {
      try { return Promise.resolve(video.play()).catch(() => false); }
      catch { return Promise.resolve(false); }
    };
    const set = (start, end, repeat) => {
      start = Number(start); end = Number(end);
      if (![start, end].every(Number.isFinite) || start < 0 || end <= start) return false;
      if (Number.isFinite(video.duration) && video.duration > 0) end = Math.min(end, video.duration);
      if (end <= start) return false;
      range = {start, end, repeat: !!repeat};
      video.currentTime = start;
      changed();
      play();
      return true;
    };
    const tick = () => {
      if (!range || video.currentTime < range.end - Math.min(0.025, (range.end - range.start) / 10)) return;
      if (range.repeat) {
        video.currentTime = range.start;
        if (video.ended || video.paused) play();
      } else {
        const end = range.end;
        clear();
        video.pause();
        video.currentTime = end;
      }
    };
    const seeking = () => {
      if (range && (video.currentTime < range.start - 0.1 || video.currentTime > range.end + 0.1)) clear();
    };
    return {set, tick, seeking, clear, get range() { return range; }};
  }

  function nativeActiveIndex(cues, time) {
    let index = -1;
    cues.forEach((cue, i) => { if (cue.start <= time + 0.025 && time < cue.end) index = i; });
    return index;
  }

  const api = { parseCues, parseTimedLine, activeIndex, nativeActiveIndex, createRangeController };
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof document === "undefined") return;

  let current = null; // Shared player, transcript cues, native cues and selected range.
  let follow = true;
  let pauseUntil = 0;
  let programmaticUntil = 0;
  let frame = 0;
  let pendingRange = 0;

  const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  function cueElement(cue) {
    const el = document.createElement("div");
    el.className = "cue";
    el.dataset.start = cue.start;
    el.dataset.end = cue.end;
    el.tabIndex = 0;
    el.title = "点击跳到这一句；点击时间可循环";
    const text = document.createElement("span");
    text.className = "cue-text";
    text.textContent = LINE.exec(cue.text)?.[2] || cue.text;
    el.append(timeButton(cue), text);
    return el;
  }

  function timeLabel(value) { return Number(value.toFixed(2)) + "s"; }
  function timeButton(cue) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cue-time timestamp-chip";
    button.textContent = `${timeLabel(cue.start)}–${timeLabel(cue.end)}`;
    button.title = "跳到此处并循环这一段";
    button.setAttribute("aria-label", `循环 ${timeLabel(cue.start)} 至 ${timeLabel(cue.end)}`);
    return button;
  }

  // 只比较字母和数字：来源拆解与当前原文常有标点、大小写上的细微差别。
  const key = (value) =>
    String(value || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();

  // 没有逐行时间戳的文案：从来源拆解文档里找到与当前原文逐字一致的"带时间转写"，
  // 一个时间段里有多句话时，按字符位置在时间段内插值出每句的起止秒数。
  function sourceSegments(item) {
    const source = item?.source_analysis?.text || "";
    const want = key(item?.current?.transcript?.text);
    if (!source || !want) return [];
    for (const match of source.matchAll(/```[^\n]*\n([\s\S]*?)```/g)) {
      const rows = match[1]
        .split(/\r?\n/)
        .filter((line) => line.trim())
        .map((line) => {
          const time = parseTimedLine(line);
          return time && { ...time, text: squash(LINE.exec(line)[2]) };
        });
      if (rows.length && rows.every(Boolean) && key(rows.map((r) => r.text).join("")) === want)
        return rows;
    }
    return [];
  }

  // units：页面上按顺序显示的每一句文字。返回与之一一对应的 {start,end}；对不上则返回 []。
  function alignUnits(units, item) {
    const segments = sourceSegments(item);
    if (!segments.length || !units.length) return [];
    let full = "";
    const spans = segments.map((seg) => {
      const from = full.length;
      full += key(seg.text);
      return { ...seg, from, to: full.length };
    });
    const timeAt = (index, atEnd) => {
      const seg =
        spans.find((s) => (atEnd ? index > s.from && index <= s.to : index >= s.from && index < s.to)) ||
        spans.at(-1);
      const frac = Math.min(1, Math.max(0, (index - seg.from) / Math.max(1, seg.to - seg.from)));
      return seg.start + frac * (seg.end - seg.start);
    };
    const result = [];
    let position = 0;
    for (const unit of units) {
      const text = key(unit);
      const at = text ? full.indexOf(text, position) : -1;
      if (at < 0) return [];
      result.push({ start: timeAt(at, false), end: timeAt(at + text.length, true) });
      position = at + text.length;
    }
    return result;
  }

  // 把纯文本容器整块换成逐句元素；已经换过则原样返回。
  function convertPlain(box, item) {
    if (box.querySelector(".cue")) return true;
    let cues = parseCues(box.textContent);
    if (!cues.length) {
      const lines = box.textContent.split(/\r?\n/).filter((line) => line.trim());
      const times = alignUnits(lines, item);
      if (times.length) cues = lines.map((text, i) => ({ ...times[i], text: text.trim() }));
    }
    if (!cues.length) return false;
    box.replaceChildren(...cues.map(cueElement));
    return true;
  }

  function preparePanel(panel, item) {
    if (!panel || panel.hidden) return false;
    if (panel.id === "transcript-original") return convertPlain(panel, item);
    const pairs = [...panel.querySelectorAll(".transcript-pair")];
    if (!pairs.length) {
      const plain = panel.querySelector(".transcript");
      return plain ? convertPlain(plain, item) : false;
    }
    const units = pairs.map((pair) => pair.querySelector(".transcript-en")?.textContent || "");
    const times = alignUnits(units, item);
    let ok = false;
    for (const [index, pair] of pairs.entries()) {
      const time = times[index] || parseTimedLine(units[index]) || (pair.classList.contains("cue") ? {start: Number(pair.dataset.start), end: Number(pair.dataset.end)} : null);
      if (!time) continue;
      pair.classList.add("cue");
      pair.dataset.start = time.start;
      pair.dataset.end = time.end;
      pair.tabIndex = 0;
      pair.title = "点击跳到这一句；点击时间可循环";
      if (!pair.querySelector(".cue-time")) pair.prepend(timeButton(time));
      const english = pair.querySelector(".transcript-en");
      const timed = LINE.exec(english?.textContent || "");
      if (timed) english.textContent = timed[2];
      ok = true;
    }
    return ok;
  }

  // 视频固定在左侧，所以滚动整个页面，让当前句始终停在屏幕中部附近。
  function scrollToCue(el) {
    const rect = el.getBoundingClientRect();
    const height = window.innerHeight;
    const dockBottom = document.querySelector(".case-dock")?.getBoundingClientRect().bottom || 0;
    const top = Math.max(height * 0.18, dockBottom + 16);
    if (rect.top >= top && rect.bottom <= height * 0.85) return;
    programmaticUntil = performance.now() + 900;
    window.scrollTo({
      top: window.scrollY + rect.top - Math.max(top, (height - Math.min(rect.height, height * 0.6)) / 2),
      behavior: reduceMotion() ? "auto" : "smooth",
    });
  }

  function update(force = false, scroll = true) {
    if (!current || !current.video.isConnected) return;
    current.controller.tick();
    updateControls();
    const index = activeIndex(current.cues, current.video.currentTime);
    const nativeIndex = nativeActiveIndex(current.nativeCues, current.video.currentTime);
    if (index === current.index && nativeIndex === current.nativeIndex && !force) return;
    current.index = index;
    current.nativeIndex = nativeIndex;
    for (const [cues, active] of [[current.cues, index], [current.nativeCues, nativeIndex]]) {
      cues.forEach((cue, i) => {
        cue.el.classList.toggle("is-active", i === active);
        if (i === active) cue.el.setAttribute("aria-current", "true");
        else cue.el.removeAttribute("aria-current");
      });
    }
    if (scroll && follow && performance.now() >= pauseUntil) {
      const cue = current.focusKind === "native" ? current.nativeCues[nativeIndex] : current.cues[index];
      if (cue && !cue.el.closest("[hidden]")) scrollToCue(cue.el);
    }
  }

  function loop() {
    frame = 0;
    const video = current?.video;
    if (!video || !video.isConnected) return;
    update();
    if (!video.paused && !video.ended) frame = requestAnimationFrame(loop);
  }

  function startLoop() {
    if (!frame) frame = requestAnimationFrame(loop);
  }

  function seekTo(el) {
    if (!current || getSelection()?.toString()) return;
    current.video.closest(".video-panel")?.dispatchEvent(new Event("show-case-video"));
    clearRange();
    setFocusKind("transcript");
    current.video.currentTime = Number(el.dataset.start) + 0.01;
    pauseUntil = 0;
    update(true);
  }

  // 用户自己滚动（滚轮、触摸、拖滚动条、方向键）后，暂停自动跟随几秒。
  function pauseFollow(event) {
    pauseUntil = performance.now() + MANUAL_SCROLL_PAUSE_MS;
    const target = event?.target;
    if (target?.closest?.("#transcript-original, #transcript-comparison")) setFocusKind("transcript");
    else if (target?.closest?.(".native-reference")) setFocusKind("native");
  }
  window.addEventListener("wheel", pauseFollow, { passive: true });
  window.addEventListener("touchmove", pauseFollow, { passive: true });
  window.addEventListener("keydown", (event) => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key))
      pauseFollow();
  });
  window.addEventListener("scroll", () => {
    if (performance.now() > programmaticUntil) pauseFollow();
  }, { passive: true });

  function bindPanel(panel) {
    if (panel.dataset.cueBound) return;
    panel.dataset.cueBound = "1";
    panel.addEventListener("click", (event) => {
      const el = event.target.closest(".cue");
      if (!el || !panel.contains(el)) return;
      if (event.target.closest(".cue-time")) playRange(Number(el.dataset.start), Number(el.dataset.end), {loop: true, kind: "transcript"});
      else seekTo(el);
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      if (event.target.closest("button")) return;
      const el = event.target.closest?.(".cue");
      if (!el) return;
      event.preventDefault();
      seekTo(el);
    });
  }

  function bindVideo(video) {
    if (video.dataset.cueBound) return;
    video.dataset.cueBound = "1";
    ["play", "playing"].forEach((name) => video.addEventListener(name, startLoop));
    ["timeupdate", "seeked", "pause", "ended"].forEach((name) =>
      video.addEventListener(name, () => update()),
    );
    // 用户主动拖进度条或点句子跳转：这是明确意图，取消手动滚动造成的暂停。
    video.addEventListener("seeking", () => {
      pauseUntil = 0;
      current?.controller.seeking();
      update();
    });
    video.addEventListener("loadedmetadata", () => { renderMarkers(); updateControls(); });
    video.addEventListener("ratechange", updateControls);
  }

  function ensureFollowButton(visible) {
    const actions = document.querySelector(".transcript-actions");
    if (!actions) return;
    let button = actions.querySelector("#toggle-transcript-follow");
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.className = "subtle";
      button.id = "toggle-transcript-follow";
      button.textContent = "跟随播放";
      button.title = "播放时文案自动滚动到当前句";
      button.onclick = () => {
        toggleFollow();
      };
      actions.prepend(button);
    }
    button.hidden = !visible;
    button.setAttribute("aria-pressed", String(follow));
    button.classList.toggle("is-active", follow);
  }

  // 有视频但文案没有句级时间时，明确告诉用户为什么不会跟随，而不是静默无效。
  function showUnsyncedNote(show) {
    let note = document.querySelector("#cue-note");
    if (!show) {
      note?.remove();
      return;
    }
    if (note) return;
    note = document.createElement("p");
    note.id = "cue-note";
    note.className = "context-note";
    note.textContent =
      "这条文案没有逐句时间，暂时无法随视频播放高亮。可在文案里按“[0.00s - 3.60s] 文字”逐行补上时间。";
    document.querySelector("#transcript-comparison")?.after(note);
  }

  function sync() {
    document.querySelectorAll("#toggle-transcript-follow, .player-follow").forEach(button => {
      button.setAttribute("aria-pressed", String(follow));
      button.classList.toggle("is-active", follow);
      button.textContent = follow ? "跟随播放：开" : "跟随播放：关";
    });
  }

  function toggleFollow() {
    follow = !follow;
    pauseUntil = 0;
    sync();
    update(true);
  }

  function clearRange() {
    pendingRange++;
    current?.controller.clear();
  }

  function playRange(start, end, options = {}) {
    if (!current || !current.video.isConnected) return false;
    current.video.closest(".video-panel")?.dispatchEvent(new Event("show-case-video"));
    const selected = current;
    const request = ++pendingRange;
    setFocusKind(options.kind || "transcript");
    pauseUntil = 0;
    const begin = () => {
      if (current !== selected || request !== pendingRange) return;
      if (!selected.controller.set(start, end, options.loop === true)) {
        if (typeof toast === "function") toast("该时间段不在视频范围内，请核对起止时间。", true);
        return;
      }
      update(true);
    };
    if (selected.video.readyState) begin();
    else selected.video.addEventListener("loadedmetadata", begin, {once: true});
    return true;
  }

  function setFocusKind(kind) {
    if (!current || current.focusKind === kind) return;
    current.focusKind = kind;
    renderMarkers();
  }

  function segmentKind() { return current?.focusKind === "transcript" && current.cues.length ? "transcript" : current?.nativeCues.length ? "native" : "transcript"; }
  function segments() { return segmentKind() === "native" ? current.nativeCues : current?.cues || []; }

  function jumpSegment(direction) {
    const cues = segments();
    if (!current || !cues.length) return;
    const index = activeIndex(cues, current.video.currentTime);
    const target = cues[Math.max(0, Math.min(cues.length - 1, index + direction))];
    playRange(target.start, target.end, {loop: false, kind: segmentKind()});
  }

  function renderMarkers() {
    const track = document.querySelector(".segment-track");
    if (!track || !current) return;
    const duration = current.video.duration;
    track.replaceChildren();
    track.setAttribute("aria-label", `${segmentKind() === "native" ? "本土表达" : "完整文案"}段落起止标记`);
    if (!Number.isFinite(duration) || duration <= 0) return;
    segments().forEach((cue, i) => {
      if (cue.start >= duration) return;
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "segment-marker";
      marker.style.left = Math.max(0, cue.start) / duration * 100 + "%";
      marker.style.width = Math.max(0.3, (Math.min(cue.end, duration) - cue.start) / duration * 100) + "%";
      marker.title = `第 ${i + 1} 段 · ${timeLabel(cue.start)}–${timeLabel(cue.end)} · 点击循环`;
      marker.setAttribute("aria-label", marker.title);
      marker.onclick = () => playRange(cue.start, cue.end, {loop: true, kind: segmentKind()});
      track.append(marker);
    });
  }

  function updateControls() {
    if (!current) return;
    const controls = document.querySelector(".segment-player-controls");
    if (!controls) return;
    const video = current.video, range = current.controller.range;
    const scrubber = controls.querySelector(".segment-scrubber");
    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    scrubber.max = Math.max(0.01, duration);
    scrubber.disabled = !duration;
    scrubber.value = video.currentTime || 0;
    scrubber.setAttribute("aria-valuetext", `${timeLabel(video.currentTime || 0)} / ${timeLabel(duration)}`);
    controls.querySelector(".segment-range-row").hidden = !range;
    const status = range ? `${range.repeat ? "循环" : "播放一次"} ${timeLabel(range.start)}–${timeLabel(range.end)}` : "";
    const statusEl = controls.querySelector(".segment-range-state");
    if (statusEl.textContent !== status) statusEl.textContent = status;
    controls.querySelector(".segment-speed").value = String(video.playbackRate);
    controls.querySelectorAll("[data-segment-jump]").forEach(button => { button.disabled = !segments().length; });
    const active = segmentKind() === "native" ? nativeActiveIndex(segments(), video.currentTime) : activeIndex(segments(), video.currentTime);
    controls.querySelectorAll(".segment-marker").forEach((marker, i) => marker.classList.toggle("is-active", i === active));
    document.querySelectorAll("[data-native-loop='true']").forEach(button => {
      const selected = !!range?.repeat && Math.abs(Number(button.dataset.nativeSeek) - range.start) < 0.001 && Math.abs(Number(button.dataset.nativeEnd) - range.end) < 0.1;
      button.setAttribute("aria-pressed", String(selected));
      button.classList.toggle("is-looping", selected);
    });
  }

  function ensurePlayerControls() {
    const wrap = current?.video.closest(".player-wrap");
    if (!wrap) return;
    let controls = document.querySelector(".segment-player-controls");
    if (!controls) {
      controls = document.createElement("div");
      controls.className = "segment-player-controls";
      controls.innerHTML = '<div class="segment-track" role="group" aria-label="段落起止标记"></div><input class="segment-scrubber" type="range" min="0" max="1" step="0.01" value="0" aria-label="视频进度"><div class="segment-toolbar"><button type="button" class="subtle" data-segment-jump="-1" title="上一段（←）" aria-label="上一段">← 上一段</button><button type="button" class="subtle" data-segment-jump="1" title="下一段（→）" aria-label="下一段">下一段 →</button><label class="segment-speed-label">倍速<select class="segment-speed" aria-label="播放速度"><option value="0.5">0.5×</option><option value="0.75">0.75×</option><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label><button type="button" class="subtle player-follow" aria-pressed="true" title="播放时自动滚动到当前段；手动滚动会暂缓跟随">跟随播放：开</button></div><div class="segment-range-row" hidden><span class="segment-range-state" role="status" aria-live="polite"></span><button type="button" class="subtle" id="clear-playback-range">取消片段</button></div>';
      wrap.after(controls);
      controls.querySelectorAll("[data-segment-jump]").forEach(button => { button.onclick = () => jumpSegment(Number(button.dataset.segmentJump)); });
      controls.querySelector(".segment-speed").onchange = event => { if (current) current.video.playbackRate = Number(event.target.value); };
      controls.querySelector(".player-follow").onclick = toggleFollow;
      controls.querySelector("#clear-playback-range").onclick = clearRange;
      controls.querySelector(".segment-scrubber").oninput = event => {
        if (!current) return;
        const time = Number(event.target.value);
        clearRange();
        current.video.currentTime = time;
        pauseUntil = 0;
        update(true);
      };
    }
    renderMarkers();
    updateControls();
    sync();
  }

  window.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || !current?.video.isConnected || current.video.closest("[hidden]")) return;
    if (event.target.closest?.('input,textarea,select,button,a,summary,video,[contenteditable="true"],[role="textbox"]') || document.querySelector("dialog[open]")) return;
    if (![" ", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === " ") {
      if (current.video.paused) current.video.play().catch(() => {});
      else current.video.pause();
    } else jumpSegment(event.key === "ArrowLeft" ? -1 : 1);
  });

  // 每次文案面板重新渲染（进入详情、切换中文对照）后调用。
  function refresh(item) {
    const video = document.querySelector("#case-player");
    const panel = [
      document.querySelector("#transcript-original"),
      document.querySelector("#transcript-comparison"),
    ].find((el) => el && !el.hidden);
    const ready = !!video && preparePanel(panel, item);
    ensureFollowButton(ready);
    showUnsyncedNote(!!video && !!panel && !ready && !!squash(panel.textContent));
    if (!video) {
      current = null;
      return;
    }
    if (ready) { panel.classList.add("cue-list"); bindPanel(panel); }
    bindVideo(video);
    const cues = ready ? [...panel.querySelectorAll(".cue")].map((el) => ({
      el,
      start: Number(el.dataset.start),
      end: Number(el.dataset.end),
    })) : [];
    const nativeCues = [...document.querySelectorAll(".native-entry[data-native-start]")].map(el => ({el, start: Number(el.dataset.nativeStart), end: Number(el.dataset.nativeEnd)})).sort((a, b) => a.start - b.start);
    const previous = current?.video === video ? current : null;
    current = { video, panel, cues, nativeCues, index: -2, nativeIndex: -2, focusKind: previous?.focusKind || (nativeCues.length ? "native" : "transcript"), controller: previous?.controller || createRangeController(video, updateControls) };
    ensurePlayerControls();
    update(true, false); // 刚进入页面只标出当前句，不抢页面滚动位置
    if (!video.paused && !video.ended) startLoop();
  }

  window.TranscriptSync = { ...api, refresh, playRange, clearRange };
})();
