'use strict';

// One compact model picker for the AI buttons that used to run on the global default without saying so
// (图文逐张表、单张改写、选题骨架). Each kind of task starts on the model last picked for it; the first option
// names that model, so what will run is visible before the click. Leaving it there sends no model and the
// server uses the kind's own choice, falling back to the global default.
window.ModelPick = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const effortNames = {none:'不启用',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高',ultra:'极致'};
  const providerNames = {claude:'Claude',codex:'Codex',antigravity:'Antigravity'};
  const picked = {};  // kind -> {model, effort}: this page's choice until the task starts
  let catalog = null, loading = null, loadedAt = 0;

  const providerOf = model => model.provider || (String(model.id).startsWith('antigravity:') ? 'antigravity' : String(model.id).startsWith('claude-') ? 'claude' : 'codex');
  const modelOf = id => (catalog?.models || []).find(model => model.id === id);
  const describe = (model,effort) => model ? `${modelOf(model)?.name || model}${effort ? ` · ${effortNames[effort] || effort}` : ''}` : '本机默认模型';
  const kindRow = kind => (catalog?.kinds || []).find(row => row.kind === kind) || {...(catalog?.defaults || {}),source:'global'};

  async function load(force = false) {
    if (catalog && !catalog.failed && !force && Date.now() - loadedAt < 45000) return catalog;
    if (loading) return loading;
    loading = (async () => {
      try {
        const result = await api('/api/ai/options');
        catalog = {models:Array.isArray(result.models) ? result.models : [],kinds:Array.isArray(result.kind_defaults) ? result.kind_defaults : [],defaults:result.default_settings || {}};
      } catch {catalog = {models:[],kinds:[],defaults:{},failed:true};}
      loadedAt = Date.now();
      return catalog;
    })();
    try {return await loading;} finally {loading = null;}
  }

  const markup = kind => `<span class="model-pick" data-model-pick="${esc(kind)}"><select data-mp-model aria-label="本次使用的模型" disabled><option value="">正在读取模型…</option></select><select data-mp-effort aria-label="本次的思考强度" disabled><option value="">模型默认</option></select></span>`;

  function paint(box) {
    const kind = box.dataset.modelPick, row = kindRow(kind), choice = picked[kind] || {};
    const select = box.querySelector('[data-mp-model]'), effort = box.querySelector('[data-mp-effort]');
    if (!select || !effort) return;
    const usable = (catalog?.models || []).filter(model => model && typeof model.id === 'string' && (model.available !== false || model.id === choice.model));
    const groups = ['claude','codex','antigravity'].map(provider => {
      const rows = usable.filter(model => providerOf(model) === provider);
      return rows.length ? `<optgroup label="${providerNames[provider]}">${rows.map(model => `<option value="${esc(model.id)}" ${model.id === choice.model ? 'selected' : ''}>${esc(model.name || model.id)}</option>`).join('')}</optgroup>` : '';
    }).join('');
    const first = catalog?.failed ? '模型列表读取失败，按默认运行' : `${row.source === 'kind' ? '沿用上次' : '全局默认'}（${describe(row.model,row.reasoning_effort)}）`;
    select.innerHTML = `<option value="">${esc(first)}</option>${groups}`;
    select.disabled = false;
    const efforts = (modelOf(choice.model)?.reasoning_efforts || []).filter(value => typeof value === 'string');
    effort.innerHTML = `<option value="">模型默认</option>${efforts.map(value => `<option value="${esc(value)}" ${value === choice.effort ? 'selected' : ''}>${esc(effortNames[value] || value)}</option>`).join('')}`;
    effort.disabled = !choice.model || !efforts.length;
    effort.hidden = !choice.model;
    select.onchange = () => {
      if (select.value) picked[kind] = {model:select.value,effort:null}; else delete picked[kind];
      document.querySelectorAll(`[data-model-pick="${kind}"]`).forEach(paint);
    };
    effort.onchange = () => {if (picked[kind]) picked[kind].effort = effort.value || null;};
  }

  // Fill every picker under root; safe to call after each re-render.
  async function mount(root = document) {
    const boxes = [...root.querySelectorAll('[data-model-pick]')];
    if (!boxes.length) return;
    await load();
    boxes.forEach(box => {if (box.isConnected) paint(box);});
  }

  // What to add to the request body: nothing when the first option is kept.
  const value = kind => picked[kind]?.model ? {model:picked[kind].model,reasoning_effort:picked[kind].effort || null} : {};

  // The task started, so the server now remembers this choice for the kind; show it as 「沿用上次」 from here on.
  function used(kind) {
    const choice = picked[kind];
    if (choice?.model && catalog) {
      const row = catalog.kinds.find(item => item.kind === kind), next = {model:choice.model,reasoning_effort:choice.effort || null,source:'kind'};
      if (row) Object.assign(row,next); else catalog.kinds.push({kind,...next});
    }
    delete picked[kind];
    document.querySelectorAll(`[data-model-pick="${kind}"]`).forEach(paint);
  }

  return {markup,mount,value,used,describe,load};
})();
