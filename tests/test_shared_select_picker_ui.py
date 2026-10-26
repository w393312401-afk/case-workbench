"""Shared select enhancement preserves form behavior and works in floating layers."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

import test_product_combo_ui as products


class SharedSelectPickerBrowserTests(unittest.TestCase):
    # Use the real application with a disposable library, not a duplicate picker.
    setUpClass = classmethod(products.ProductComboBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(products.ProductComboBrowserTests.tearDownClass.__func__)
    setUp = products.ProductComboBrowserTests.setUp
    stop_server = products.ProductComboBrowserTests.stop_server
    request = products.ProductComboBrowserTests.request
    make_playable_video = products.ProductComboBrowserTests.make_playable_video
    assert_no_overflow = products.ProductComboBrowserTests.assert_no_overflow
    wait_peek = products.ProductComboBrowserTests.wait_peek
    assert_in_viewport = products.ProductComboBrowserTests.assert_in_viewport

    def open_page(self, *, width=1280, height=900, theme="light", touch=False):
        context = self.browser.new_context(
            viewport={"width": width, "height": height}, has_touch=touch,
            color_scheme=theme,
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(self.base + "/#/202H/library")
        page.locator("#case-search").wait_for()
        page.wait_for_function("() => typeof WorkbenchShell.enhanceSelects === 'function'")
        return page

    def mount(self, page, markup, *, modal=False):
        page.evaluate("""({markup, modal}) => {
            document.querySelector('#shared-select-fixture')?.remove();
            const host = document.createElement(modal ? 'dialog' : 'div');
            host.id = 'shared-select-fixture';
            host.style.cssText = modal
                ? 'width:min(400px, calc(100vw - 24px));padding:16px;'
                : 'position:fixed;left:12px;top:110px;width:min(380px, calc(100vw - 24px));padding:12px;z-index:1000;background:var(--surface);';
            host.innerHTML = markup;
            document.body.append(host);
            if (modal) host.showModal();
        }""", {"markup": markup, "modal": modal})
        # New view markup is enhanced automatically, including inside dialogs.
        page.locator("#shared-select-fixture select + .workbench-select").first.wait_for()
        return page.locator("#shared-select-fixture")

    def trigger(self, page, identifier="fixture-select"):
        return page.locator("#" + identifier + " + .workbench-select [data-select-trigger]")

    def open_picker(self, page, identifier="fixture-select"):
        trigger = self.trigger(page, identifier)
        trigger.click()
        target = trigger.get_attribute("aria-controls")
        self.assertTrue(target)
        listing = page.locator("[id=" + json.dumps(target) + "]")
        panel = listing.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' workbench-select-panel ')]")
        expect(panel).to_be_visible()
        expect(panel.locator(".product-picker-search")).to_be_focused()
        return panel

    def test_search_keeps_the_empty_value_choice_and_delegates_native_input_and_change_once(self):
        page = self.open_page()
        fixture = self.mount(page, """
            <form id="fixture-form"><label for="fixture-select">筛选状态</label>
            <select id="fixture-select" name="status">
                <option value="">全部状态</option><option value="draft">草稿 Draft</option>
                <option value="published">已发布 Published</option>
            </select></form>
        """)
        page.evaluate("""() => {
            window.fixtureEvents = [];
            for (const type of ['input', 'change']) document.querySelector('#fixture-form')
                .addEventListener(type, event => {
                    if (event.target.matches('select')) fixtureEvents.push([event.type, event.target.value]);
                });
        }""")
        panel = self.open_picker(page)
        search = panel.locator(".product-picker-search")
        search.fill("published 已发布")
        expect(panel.locator("[data-picker-value]")).to_have_count(1)
        panel.locator('[data-picker-value="published"]').click()
        expect(fixture.locator("select")).to_have_value("published")
        expect(self.trigger(page)).to_contain_text("已发布 Published")
        self.assertEqual(page.evaluate("fixtureEvents"), [["input", "published"], ["change", "published"]])
        self.assertEqual(page.evaluate("new FormData(document.querySelector('#fixture-form')).get('status')"), "published")
        panel = self.open_picker(page)
        panel.locator(".product-picker-search").fill("全部状态")
        expect(panel.locator("[data-picker-value]")).to_have_count(1)
        panel.locator('[data-picker-value=""]').click()
        expect(fixture.locator("select")).to_have_value("")
        self.assertEqual(page.evaluate("fixtureEvents"), [
            ["input", "published"], ["change", "published"], ["input", ""], ["change", ""],
        ])
        # Selecting the current item has native select semantics: no new change.
        self.open_picker(page).locator('[data-picker-value=""]').click()
        self.assertEqual(len(page.evaluate("fixtureEvents")), 4)

    def test_dynamic_options_values_and_disabled_fields_preserve_native_metadata(self):
        page = self.open_page()
        self.mount(page, """
            <fieldset id="fixture-fieldset"><label for="fixture-select">内容类型</label>
            <select id="fixture-select"><option value="">全部类型</option>
                <option value="ready">可用</option><option value="blocked" disabled>暂不可用</option>
                <optgroup label="停用分组" disabled><option value="group-blocked">分组停用项</option></optgroup>
            </select></fieldset><select id="fixture-multiple" multiple><option>甲</option><option>乙</option></select>
            <select id="fixture-list" size="3"><option>甲</option><option>乙</option></select>
        """)
        expect(page.locator("#fixture-multiple + .workbench-select")).to_have_count(0)
        expect(page.locator("#fixture-list + .workbench-select")).to_have_count(0)
        panel = self.open_picker(page)
        blocked = panel.locator('[data-picker-value="blocked"]')
        expect(blocked).to_have_attribute("aria-disabled", "true")
        expect(panel.locator('[data-picker-value="group-blocked"]')).to_have_attribute("aria-disabled", "true")
        blocked.click(force=True)
        expect(page.locator("#fixture-select")).to_have_value("")
        expect(panel).to_be_visible()
        page.keyboard.press("End")
        page.keyboard.press("Enter")
        expect(page.locator("#fixture-select")).to_have_value("ready")
        self.assertTrue(page.locator('#fixture-select option[value="blocked"]').is_disabled())
        self.assertTrue(page.locator("#fixture-select optgroup").is_disabled())
        page.evaluate("""() => {
            const select = document.querySelector('#fixture-select');
            select.options[1].textContent = '可以发布';
            select.add(new Option('配音 Audio', 'audio'));
            select.value = 'audio';
        }""")
        expect(self.trigger(page)).to_contain_text("配音 Audio")
        page.evaluate("document.querySelector('#fixture-select').selectedIndex = 1")
        expect(self.trigger(page)).to_contain_text("可以发布")
        page.evaluate("document.querySelector('#fixture-select option[value=audio]').selected = true")
        expect(self.trigger(page)).to_contain_text("配音 Audio")
        panel = self.open_picker(page)
        expect(panel.locator('[data-picker-value="ready"]')).to_contain_text("可以发布")
        expect(panel.locator('[data-picker-value="audio"]')).to_have_attribute("aria-selected", "true")
        page.evaluate("document.querySelector('#fixture-select').disabled = true")
        expect(self.trigger(page)).to_be_disabled()
        expect(panel).to_be_hidden()
        page.evaluate("""() => {
            document.querySelector('#fixture-select').disabled = false;
            document.querySelector('#fixture-fieldset').disabled = true;
        }""")
        expect(self.trigger(page)).to_be_disabled()
        page.evaluate("document.querySelector('#fixture-fieldset').disabled = false")
        expect(self.trigger(page)).to_be_enabled()
        page.evaluate("document.querySelector('#fixture-select option[value=audio]').remove()")
        expect(self.trigger(page)).to_contain_text("全部类型")

    def test_required_form_validation_focus_and_reset_remain_usable(self):
        page = self.open_page()
        self.mount(page, """
            <form id="fixture-form"><label for="fixture-select">选择用途</label>
            <select id="fixture-select" name="purpose" required>
                <option value="" selected>请选择用途</option><option value="review">复盘</option>
            </select><button id="fixture-submit">保存</button>
                <button id="fixture-reset" type="reset">重置</button></form>
        """)
        page.evaluate("""() => {
            window.fixtureSubmits = 0;
            document.querySelector('#fixture-form').addEventListener('submit', event => {
                event.preventDefault(); fixtureSubmits++;
            });
        }""")
        page.locator("#fixture-submit").click()
        self.assertEqual(page.evaluate("fixtureSubmits"), 0)
        self.assertFalse(page.locator("#fixture-select").evaluate("node => node.checkValidity()"))
        expect(self.trigger(page)).to_be_focused()
        self.open_picker(page).locator('[data-picker-value="review"]').click()
        page.locator("#fixture-submit").click()
        self.assertEqual(page.evaluate("fixtureSubmits"), 1)
        page.locator("#fixture-reset").click()
        expect(page.locator("#fixture-select")).to_have_value("")
        expect(self.trigger(page)).to_contain_text("请选择用途")
        self.assertEqual(page.locator("#fixture-select option[selected]").get_attribute("value"), "")

    def test_removal_and_repeated_rendering_release_floating_panels_and_global_listeners(self):
        page = self.open_page()
        page.evaluate("""() => {
            window.fixtureListeners = [];
            for (const target of [document, window, window.visualViewport].filter(Boolean)) {
                const add = target.addEventListener.bind(target);
                const remove = target.removeEventListener.bind(target);
                target.addEventListener = function(type, listener, options) {
                    const row = {target, type, listener, capture: !!(typeof options === 'boolean' ? options : options?.capture), live: true};
                    fixtureListeners.push(row);
                    options?.signal?.addEventListener('abort', () => { row.live = false; }, {once: true});
                    return add(type, listener, options);
                };
                target.removeEventListener = function(type, listener, options) {
                    const capture = !!(typeof options === 'boolean' ? options : options?.capture);
                    fixtureListeners.filter(row => row.type === type && row.listener === listener && row.capture === capture)
                        .forEach(row => { row.live = false; });
                    return remove(type, listener, options);
                };
            }
        }""")
        baseline = page.locator(".workbench-select-panel").count()
        markup = '<label for="fixture-select">状态</label><select id="fixture-select"><option>A</option><option>B</option></select>'
        for _ in range(4):
            self.mount(page, markup)
            page.evaluate("WorkbenchShell.enhanceSelects(document); WorkbenchShell.enhanceSelects(document)")
            expect(page.locator("#fixture-select + .workbench-select")).to_have_count(1)
            panel = self.open_picker(page)
            self.assertEqual(page.locator(".workbench-select-panel").count(), baseline + 1)
            page.evaluate("document.querySelector('#shared-select-fixture').remove()")
            expect(panel).to_have_count(0)
            expect(page.locator(".workbench-select-panel")).to_have_count(baseline)
            page.wait_for_function("() => fixtureListeners.every(row => !row.live)")
        self.assertEqual(page.locator(".product-picker-panel:popover-open").count(), 0)

    def test_modal_popovers_are_exclusive_and_keep_keyboard_ime_and_escape_focus(self):
        page = self.open_page(width=390)
        modal = self.mount(page, """
            <label for="fixture-select">状态</label><select id="fixture-select">
                <option value="">全部</option><option value="draft">草稿</option><option value="done">完成</option>
            </select><label for="fixture-other">类型</label><select id="fixture-other">
                <option value="">全部类型</option><option value="image">图片</option>
            </select>
        """, modal=True)
        panel = self.open_picker(page)
        self.assertTrue(panel.evaluate("node => node.matches(':popover-open')"))
        other = self.open_picker(page, "fixture-other")
        expect(panel).to_be_hidden()
        self.assertEqual(page.locator(".product-picker-panel:popover-open").count(), 1)
        page.keyboard.press("Escape")
        expect(other).to_be_hidden()
        expect(modal).to_be_visible()
        expect(self.trigger(page, "fixture-other")).to_be_focused()
        trigger = self.trigger(page)
        trigger.focus()
        page.keyboard.press("ArrowDown")
        panel = page.locator(".workbench-select-panel:popover-open")
        search = panel.locator(".product-picker-search")
        expect(search).to_be_focused()
        search.fill("草稿")
        search.evaluate("""node => node.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Enter', isComposing: true, bubbles: true, cancelable: true,
        }))""")
        expect(panel).to_be_visible()
        expect(page.locator("#fixture-select")).to_have_value("")
        page.keyboard.press("Enter")
        expect(page.locator("#fixture-select")).to_have_value("draft")
        expect(trigger).to_be_focused()
        panel = self.open_picker(page)
        page.keyboard.press("Home")
        self.assertEqual(search.get_attribute("aria-activedescendant"), panel.locator("[data-picker-value]").first.get_attribute("id"))
        page.keyboard.press("End")
        self.assertEqual(search.get_attribute("aria-activedescendant"), panel.locator("[data-picker-value]").last.get_attribute("id"))
        page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        expect(modal).to_be_visible()
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()

    def test_touch_and_long_options_fit_portrait_and_landscape_in_both_themes(self):
        text = "厨房便携磨刀器产品说明和用户反馈，长选项仍能完整展示并选择"
        for width, height in ((320, 740), (667, 375)):
            for theme in ("light", "dark"):
                with self.subTest(width=width, height=height, theme=theme):
                    page = self.open_page(width=width, height=height, theme=theme, touch=True)
                    self.mount(page, '<label for="fixture-select">内容</label><select id="fixture-select">'
                               '<option value="">全部内容</option><option value="long">' + text + '</option></select>')
                    panel = self.open_picker(page)
                    self.assert_in_viewport(page, panel)
                    self.assertTrue(panel.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
                    option = panel.locator('[data-picker-value="long"]')
                    option.scroll_into_view_if_needed()
                    option.tap()
                    expect(page.locator("#fixture-select")).to_have_value("long")
                    expect(panel).to_be_hidden()
                    expect(self.trigger(page)).to_contain_text(text)
                    self.assert_no_overflow(page)
                    page.close()


if __name__ == "__main__":
    unittest.main()
