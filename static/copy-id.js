'use strict';

// A published media ID shown as text or beside its TikTok link. One delegated handler serves every button,
// so a re-rendered panel needs no rewiring. copyText comes from the app shell.
const copyIdEscape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
// icon: a square copy-icon button (with its label as tooltip) for compact rows; otherwise the text button.
function copyIdButton(id, label = 'ID', {icon: asIcon = false} = {}) {
  const value = String(id ?? '').trim();
  if (!value) return '';
  const look = asIcon && typeof icon === 'function' ? ['icon-action copy-id', icon('copy')] : ['subtle compact copy-id', '复制'];
  return ` <button type="button" class="${look[0]}" data-copy-id="${copyIdEscape(value)}" data-copy-label="${copyIdEscape(label)}" title="复制${copyIdEscape(label)}" aria-label="复制${copyIdEscape(label)} ${copyIdEscape(value)}">${look[1]}</button>`;
}
// The TikTok link that sits beside a copied ID, as the matching square icon; label names it for screen readers.
function openLinkButton(url, label = '原视频', title = '在 TikTok 打开', extraClass = '') {
  const value = String(url ?? '').trim();
  if (!value) return '';
  return `<a class="icon-action${extraClass ? ` ${copyIdEscape(extraClass)}` : ''}" href="${copyIdEscape(value)}" target="_blank" rel="noopener noreferrer" title="${copyIdEscape(title)}" aria-label="${copyIdEscape(label)}">${typeof icon === 'function' ? icon('external') : '↗'}</a>`;
}
document.addEventListener('click', event => {
  const button = event.target.closest?.('[data-copy-id]');
  if (!button) return;
  event.preventDefault();
  void copyText(button.dataset.copyId, `${button.dataset.copyLabel || 'ID'}已复制`);
  // An icon button shows a tick in place for a moment, where the eye already is.
  if (button.classList.contains('icon-action') && typeof icon === 'function' && !button.dataset.copied) {
    const original = button.innerHTML;
    button.dataset.copied = '1';
    button.innerHTML = icon('check');
    setTimeout(() => {button.innerHTML = original;delete button.dataset.copied;}, 1200);
  }
});
