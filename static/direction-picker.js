"use strict";

// Shared catalog selection for case snapshots and independent video assignments.
window.CaseDirectionPicker = (() => {
  const bindings = new WeakMap();
  const canonical = value => window.WorkbenchShell?.normalizeProduct(value) || String(value || "").trim();
  const productOf = item => canonical(item?.product || "");
  const sameProduct = (a, b) => canonical(a).toLocaleLowerCase() === canonical(b).toLocaleLowerCase();
  const fetchCatalog = (product = "", options = {}) => api(`/api/direction-catalog${product ? `?product=${encodeURIComponent(canonical(product))}` : ""}`, options);
  const findDirection = (catalog, product, id) => catalog?.directions?.find(d => sameProduct(d.product, product) && d.direction_id === id);
  const findLink = (catalog, product, video) => catalog?.video_links?.find(link => sameProduct(link.product, product) && link.video_id === video);
  function choices(catalog, product, selected = "") {
    return (catalog?.directions || []).filter(d => sameProduct(d.product, product) && (d.status !== "archived" || d.direction_id === selected)).sort((a, b) => a.direction_id.localeCompare(b.direction_id, "zh-CN", {numeric: true}));
  }
  function optionMarkup(directions, selected, unknownLabel = "历史记录（保留）") {
    const unknown = selected && !directions.some(d => d.direction_id === selected);
    return '<option value="">不关联方向</option>' + (unknown ? `<option value="${esc(selected)}" selected>${esc(selected)} · ${unknownLabel}</option>` : "") + directions.map(d => `<option value="${esc(d.direction_id)}" ${d.direction_id === selected ? "selected" : ""}>${esc(d.direction_id)} · ${esc(d.main_difference)}${d.status === "archived" ? "（已归档）" : d.status === "paused" ? "（已暂停）" : ""}</option>`).join("");
  }
  function markup(current) {
    const d = current.test_direction || {};
    return `<section class="editor-section test-direction-editor direction-picker"><h3>测试方向（可选）</h3><p>选择这个产品已有的方向。顾虑链、收口和执行改动记录这条案例的实际做法；取消关联只移除当前标记，历史仍保留。</p><label class="field"><span>已有方向</span><select name="direction_direction_id" aria-label="已有方向">${optionMarkup([], d.direction_id)}</select></label><div class="direction-picker-meta"><p data-direction-status role="status">正在读取方向…</p><button type="button" class="subtle" data-direction-retry hidden>重试</button><a data-direction-manage target="_blank" rel="noopener">管理方向</a></div>${field("direction_main_difference", "主打卖点", d.main_difference || "", {readonly: true, help: "随所选方向填写；方向本身在测试方向中管理。"})}${field("direction_variant_type", "变体类型", d.variant_type || "direction", {select: directionVariantNames})}${field("direction_concern", "本案例顾虑链", d.concern || "", {area: true})}${field("direction_closing", "本案例收口", d.closing || "", {area: true})}${field("direction_execution_change", "执行改动", d.execution_change || "", {help: "说明这条案例改了哪些钩子、顺序、语气或镜头。"})}${field("direction_script_version", "方向对应文案版本", d.script_version || "", {help: "已有文案版本保持原样，请按这条案例实际使用的版本核对。"})}</section>`;
  }
  function bind(form, item) {
    const host = form.querySelector(".direction-picker");
    if (!host) return;
    const get = name => form.querySelector(`[name="direction_${name}"]`), select = get("direction_id"), status = host.querySelector("[data-direction-status]"), retry = host.querySelector("[data-direction-retry]");
    const initial = structuredClone(item.current?.test_direction || {}), initialProduct = productOf(item), productField = form.querySelector('[name="product"]');
    const binding = {catalog: null, product: canonical(productField?.value || initialProduct), initialProduct, initial, sequence: 0, controller: null};
    bindings.set(form, binding);
    function setStatus(message, error = false) {status.textContent = message; status.classList.toggle("is-error", error);}
    async function load() {
      binding.controller?.abort(); binding.controller = new AbortController();
      const seq = ++binding.sequence, product = canonical(productField?.value || initialProduct);
      if (!sameProduct(product, binding.product)) {select.value = ""; get("main_difference").value = ""; binding.catalog = null;}
      binding.product = product; retry.hidden = true; select.setAttribute("aria-busy", "true");
      host.querySelector("[data-direction-manage]").href = window.WorkbenchShell?.url("#directions", product) || `#directions?product=${encodeURIComponent(product)}`;
      setStatus("正在读取方向…");
      try {
        const catalog = await fetchCatalog(product, {signal: binding.controller.signal});
        if (seq !== binding.sequence || !form.isConnected) return;
        binding.catalog = catalog;
        const selected = select.value, list = choices(catalog, product, selected);
        select.innerHTML = optionMarkup(list, selected);
        setStatus(list.length ? `可选 ${list.length} 个方向；已有执行记录保持原样。` : "这个产品还没有方向，可先到测试方向中新建。旧记录可以继续保留。");
      } catch (error) {
        if (error.name === "AbortError" || seq !== binding.sequence || !form.isConnected) return;
        retry.hidden = false; setStatus(`方向暂不可用：${error.message}。已有记录仍可保留。`, true);
      } finally {if (seq === binding.sequence) select.removeAttribute("aria-busy");}
    }
    select.addEventListener("change", () => {
      const direction = findDirection(binding.catalog, binding.product, select.value);
      if (!select.value) {get("main_difference").value = ""; setStatus("保存后将取消当前方向关联，历史记录仍保留。"); return;}
      if (!direction) return;
      get("main_difference").value = direction.direction_id === initial.direction_id && sameProduct(binding.product, initialProduct) ? initial.main_difference : direction.main_difference;
      for (const key of ["concern", "closing"]) if (!get(key).value.trim()) get(key).value = direction[key] || "";
      setStatus("方向已选择。请核对本案例顾虑链、收口及对应文案版本。");
    });
    productField?.addEventListener("change", load); retry.onclick = load; void load();
  }
  function collect(current, value, form) {
    const id = value("direction_direction_id");
    if (!id) {delete current.test_direction; return;}
    const binding = bindings.get(form), initial = binding?.initial || current.test_direction || {};
    const unchanged = id === initial.direction_id && (!binding || sameProduct(binding.product, binding.initialProduct));
    const direction = findDirection(binding?.catalog, binding?.product, id);
    if (!unchanged && (!direction || direction.status === "archived")) throw new Error("请选择这个产品已有且未归档的方向，或重试读取方向。");
    const result = {...(current.test_direction || {}), direction_id: id};
    for (const key of ["main_difference", "concern", "closing", "execution_change", "script_version"]) {
      const text = key === "main_difference" && !unchanged ? direction.main_difference : value("direction_" + key);
      if (text) result[key] = text; else delete result[key];
    }
    for (const key of ["main_difference", "concern", "closing"]) if (!result[key]) throw new Error(`测试方向请补齐“${directionFieldLabels[key]}”，或选择“不关联方向”。`);
    result.variant_type = value("direction_variant_type");
    if (!Object.hasOwn(directionVariantNames, result.variant_type)) throw new Error("请选择有效的变体类型。");
    current.test_direction = result;
  }
  function videoCell(video, catalog) {
    const link = findLink(catalog, video.product, video.video_id), label = link?.direction_id || (catalog ? "未归类" : "读取方向");
    return `<td class="perf-direction" data-direction-video="${esc(video.video_id)}"><button type="button" class="subtle" data-video-direction aria-label="选择视频 ${esc(video.video_id)} 的方向">${esc(label)}</button></td>`;
  }
  function bindVideos(root, videos, getCatalog, onSaved) {
    root.querySelectorAll("[data-video-direction]").forEach(button => button.onclick = async () => {
      const cell = button.closest("[data-direction-video]"), video = videos.find(v => v.video_id === cell.dataset.directionVideo);
      if (!video) return;
      const product = canonical(video.product);
      const restore = () => {if (!cell.isConnected) return; const holder = document.createElement("table"); holder.innerHTML = `<tbody><tr>${videoCell(video, getCatalog())}</tr></tbody>`; const next = holder.querySelector("td"); cell.replaceWith(next); bindVideos(next, videos, getCatalog, onSaved); next.querySelector("button").focus();};
      cell.dataset.directionEditing = "true";
      cell.innerHTML = '<p role="status">正在读取方向…</p>';
      let catalog;
      try {catalog = await fetchCatalog(product);}
      catch (error) {if (cell.isConnected) {cell.innerHTML = `<p class="is-error" role="status">${esc(error.message)}</p><button type="button" class="subtle">返回后重试</button>`; cell.querySelector("button").onclick = restore;} return;}
      if (!cell.isConnected) return;
      const assigned = findLink(catalog, product, video.video_id)?.direction_id || "";
      cell.innerHTML = `<form class="video-direction-editor" aria-label="视频 ${esc(video.video_id)} 的方向"><label><span>测试方向</span><select aria-label="视频测试方向">${optionMarkup(choices(catalog, product, assigned), assigned, "当前关联不可用")}</select></label><div class="video-direction-actions"><button type="submit" class="primary">保存</button>${assigned ? '<button type="button" class="subtle" data-video-unlink>解除关联</button>' : ""}<button type="button" class="subtle" data-video-cancel>取消</button></div><p role="status">${esc(product)} · 选择后保存${video.case_id ? "；更换前先解除视频关联，在案例修改方向后再绑定" : ""}</p></form>`;
      const form = cell.querySelector("form"), select = form.querySelector("select"), status = form.querySelector('[role="status"]');
      let fingerprint = "", requestId = "";
      form.querySelector("[data-video-cancel]").onclick = restore;
      form.querySelector("[data-video-unlink]")?.addEventListener("click", () => {select.value = ""; status.textContent = "已选择解除关联，点击保存生效。"; select.focus();});
      form.onsubmit = async event => {
        event.preventDefault(); const id = select.value;
        if (id && (!findDirection(catalog, product, id) || (id !== assigned && findDirection(catalog, product, id).status === "archived"))) {status.textContent = "这个方向已不可用，请重新选择。"; return;}
        const controls = [...form.querySelectorAll("button,select")]; controls.forEach(control => control.disabled = true); status.classList.remove("is-error"); status.textContent = "正在保存…";
        try {
          const body = {revision: catalog.revision, video_id: video.video_id, product, direction_id: id}, nextFingerprint = JSON.stringify(body);
          if (fingerprint !== nextFingerprint) {fingerprint = nextFingerprint; requestId = crypto.randomUUID();}
          const result = await api("/api/video-directions", {method: "POST", body: {...body, request_id: requestId}});
          onSaved(result.catalog, result.link);
          window.dispatchEvent(new CustomEvent("workbench:directionchange", {detail: {product, video_id: video.video_id}}));
          restore(); toast(id ? "视频方向已保存" : "视频方向已解除关联");
        } catch (error) {
          if (!form.isConnected) return;
          status.classList.add("is-error"); status.textContent = error.message;
          if (error.status === 409) {
            try {catalog = await fetchCatalog(product); if (!form.isConnected) return; select.innerHTML = optionMarkup(choices(catalog, product, id), id, "已不可用"); status.textContent = `${error.message} 已刷新方向，保留你的选择；核对后可再次保存。`;}
            catch (refreshError) {status.textContent = `${error.message} 刷新失败：${refreshError.message}。请取消后重试。`;}
          }
        } finally {if (form.isConnected) controls.forEach(control => control.disabled = false);}
      };
      select.focus();
    });
  }
  function inheritNotice(video, catalog, form) {
    const link = findLink(catalog, video.product, video.video_id);
    if (!link?.direction_id || !form) return;
    const note = document.createElement("p"); note.className = "direction-inherit-note"; note.setAttribute("role", "status");
    note.textContent = `保存后将继承视频方向 ${link.direction_id}，顾虑链和执行变体可在案例中继续补充。`;
    (form.querySelector(".quick-create-fields") || form.querySelector("#editor-fields") || form).prepend(note);
  }
  return {markup, bind, collect, fetchCatalog, videoCell, bindVideos, inheritNotice, findLink, hasVideoEdits: () => !!document.querySelector('[data-direction-editing="true"]')};
})();
