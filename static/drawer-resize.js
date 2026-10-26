'use strict';

// Drag the left edge of a right-hand drawer to resize it. Each kind of drawer remembers its own width in this browser.
window.DrawerResize = (() => {
  const MIN = 360, STEP = 24;
  const key = kind => `workbench-drawer-width:${kind}`;
  const limit = () => Math.max(MIN, Math.min(Math.floor(innerWidth * .92), 1200));
  const clamp = value => Math.max(MIN, Math.min(Math.round(value), limit()));
  function saved(kind) {
    try {
      const value = Number(localStorage.getItem(key(kind)));
      return Number.isFinite(value) && value >= MIN ? clamp(value) : 0;
    } catch { return 0; }
  }
  function remember(kind, width) {
    try {
      if (width) localStorage.setItem(key(kind), String(width));
      else localStorage.removeItem(key(kind));
    } catch { /* The width simply is not remembered without browser storage. */ }
  }
  const apply = (dialog, width) => width ? dialog.style.setProperty('--drawer-width', clamp(width) + 'px') : dialog.style.removeProperty('--drawer-width');
  const current = dialog => Math.round(dialog.getBoundingClientRect().width);

  // Call after every render of the drawer: the handle is part of what innerHTML replaces.
  function attach(dialog, kind = 'default') {
    apply(dialog, saved(kind));
    dialog.querySelector(':scope > .drawer-resize')?.remove();
    const handle = document.createElement('div');
    handle.className = 'drawer-resize';
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', 'vertical');
    handle.setAttribute('aria-label', '调整侧栏宽度');
    handle.title = '左右拖动调整宽度，双击恢复默认';
    handle.tabIndex = 0;
    let drag = null;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      drag = {x: event.clientX, width: current(dialog)};
      dialog.classList.add('is-resizing');
    });
    handle.addEventListener('pointermove', event => {
      if (drag) apply(dialog, drag.width + drag.x - event.clientX);
    });
    const finish = () => {
      if (!drag) return;
      drag = null;
      dialog.classList.remove('is-resizing');
      remember(kind, current(dialog));
    };
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
    handle.addEventListener('keydown', event => {
      const direction = {ArrowLeft: 1, ArrowRight: -1}[event.key];
      if (!direction && event.key !== 'Home') return;
      event.preventDefault();
      if (direction) {
        apply(dialog, current(dialog) + direction * STEP);
        remember(kind, current(dialog));
      } else {
        apply(dialog, 0);
        remember(kind, 0);
      }
    });
    handle.addEventListener('dblclick', () => {
      apply(dialog, 0);
      remember(kind, 0);
    });
    dialog.prepend(handle);
  }
  return {attach, saved};
})();
