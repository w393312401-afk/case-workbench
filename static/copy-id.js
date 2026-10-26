'use strict';

// A published media ID shown as text or beside its TikTok link. One delegated handler serves every button,
// so a re-rendered panel needs no rewiring. copyText comes from the app shell.
const copyIdEscape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function copyIdButton(id, label = 'ID') {
  const value = String(id ?? '').trim();
  if (!value) return '';
  return ` <button type="button" class="subtle compact copy-id" data-copy-id="${copyIdEscape(value)}" data-copy-label="${copyIdEscape(label)}" title="复制${copyIdEscape(label)}" aria-label="复制${copyIdEscape(label)} ${copyIdEscape(value)}">复制</button>`;
}
document.addEventListener('click', event => {
  const button = event.target.closest?.('[data-copy-id]');
  if (!button) return;
  event.preventDefault();
  void copyText(button.dataset.copyId, `${button.dataset.copyLabel || 'ID'}已复制`);
});
