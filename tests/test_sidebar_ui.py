"""Sidebar interactions against the shell's disposable library and browser profile."""
from __future__ import annotations

import json
import unittest

import test_shell_ui as shell


class SidebarBrowserTests(unittest.TestCase):
    # Reuse the isolated fixture without inheriting its unrelated test methods.
    setUpClass = classmethod(shell.ShellBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(shell.ShellBrowserTests.tearDownClass.__func__)
    setUp = shell.ShellBrowserTests.setUp
    stop_server = shell.ShellBrowserTests.stop_server
    request = shell.ShellBrowserTests.request
    make_playable_video = shell.ShellBrowserTests.make_playable_video
    assert_route = shell.ShellBrowserTests.assert_route
    assert_no_overflow = shell.ShellBrowserTests.assert_no_overflow

    def open_page(self, route="#library", *, width=1280, preference=None, touch=False):
        context = self.browser.new_context(
            viewport={"width": width, "height": 900}, has_touch=touch,
        )
        self.addCleanup(context.close)
        if preference is not None:
            # Set only the initial preference, so reloads can verify later writes.
            context.add_init_script(
                "if (localStorage.getItem('workbench-sidebar-v1') === null) "
                "localStorage.setItem('workbench-sidebar-v1', " + json.dumps(preference) + ");"
            )
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(self.base + "/" + route)
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        page.locator("#sidebar-toggle").wait_for(state="attached")
        return page

    def wait_collapsed(self, page, collapsed=True):
        page.wait_for_function(
            "expected => document.documentElement.classList.contains('sidebar-collapsed') === expected",
            arg=collapsed,
        )

    def wait_peek(self, page, opened=True):
        page.wait_for_function(
            "expected => document.documentElement.classList.contains('sidebar-peek') === expected",
            arg=opened,
        )

    def rail_button(self, page, target):
        return page.locator(f'#sidebar-rail [data-sidebar-target="{target}"]')

    def assert_sidebar_icons_aligned(self, page, *, visible_only=False):
        # Native scrollIntoView/focus dispatches its scroll event on the next frame.
        page.wait_for_function("""() => Math.abs(
          document.querySelector('#sidebar-rail').scrollTop -
          document.querySelector('#workbench-sidebar').scrollTop
        ) <= 1""")
        rows = page.evaluate("""() => {
          const rail = document.querySelector('#sidebar-rail').getBoundingClientRect();
          const panel = document.querySelector('#workbench-sidebar').getBoundingClientRect();
          return [...document.querySelectorAll('#sidebar-rail [data-sidebar-target]')].map(button => {
            const source = document.getElementById(button.dataset.sidebarTarget);
            const left = button.querySelector('[data-icon]').getBoundingClientRect();
            const right = source.querySelector('[data-icon]').getBoundingClientRect();
            return {
              target: source.id, left: left.y + left.height / 2, right: right.y + right.height / 2,
              visible: !button.hidden && !source.hidden && left.height > 0 && right.height > 0 &&
                left.top >= rail.top && left.bottom <= Math.min(rail.bottom, innerHeight) &&
                right.top >= panel.top && right.bottom <= Math.min(panel.bottom, innerHeight),
            };
          });
        }""")
        checked = [row for row in rows if row["visible"]] if visible_only else rows
        self.assertGreater(len(checked), 0)
        if not visible_only:
            self.assertEqual(len(checked), 8)
            self.assertTrue(all(row["visible"] for row in checked))
        for row in checked:
            with self.subTest(aligned_module=row["target"]):
                self.assertAlmostEqual(row["left"], row["right"], delta=1)

    def test_toggle_pins_and_persists_the_desktop_preference(self):
        page = self.open_page()
        page.locator("#case-search").wait_for()
        self.wait_collapsed(page, False)
        self.assertTrue(page.locator(".topbar #sidebar-toggle").is_visible())
        self.assertEqual(page.locator("#sidebar-toggle:visible, #sidebar-menu:visible").count(), 1)
        self.assertTrue(page.locator("#workbench-sidebar").is_visible())
        self.assertTrue(page.locator("#sidebar-rail").is_hidden())
        self.assertEqual(page.locator(".workflow-nav .nav-item").all_inner_texts(), [
            "工作概览", "用户声音", "卖点与方向", "内容创作", "带货数据", "案例库",
        ])
        workflow = page.locator(".workflow-nav").bounding_box()
        tools = page.locator(".sidebar-tools").bounding_box()
        self.assertGreater(tools["y"], workflow["y"] + workflow["height"] + 20)
        expanded = page.locator("main").bounding_box()
        page.locator("#sidebar-toggle").click()
        self.wait_collapsed(page)
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-sidebar-v1')"), "collapsed")
        self.assertTrue(page.locator("#sidebar-rail").is_visible())
        self.assertTrue(page.locator("#workbench-sidebar").is_hidden())
        self.assertTrue(page.locator("#sidebar-toggle").is_hidden())
        self.assertEqual(page.locator("#sidebar-toggle:visible, #sidebar-menu:visible").count(), 1)
        self.assertEqual(page.evaluate("document.activeElement.id"), "sidebar-menu")
        rail_workflow = page.locator("#sidebar-rail nav").bounding_box()
        rail_tools = page.locator(".sidebar-rail-tools").bounding_box()
        self.assertGreater(rail_tools["y"], rail_workflow["y"] + rail_workflow["height"] + 20)
        self.assertLess(page.locator("main").bounding_box()["x"], expanded["x"])
        page.reload()
        page.locator("#case-search").wait_for()
        self.wait_collapsed(page)
        self.rail_button(page, "library-nav").hover()
        self.wait_peek(page)
        self.assertEqual(page.locator("#sidebar-pin").inner_text().strip(), "固定")
        self.assertEqual(page.locator("#sidebar-pin svg").count(), 0)
        self.assertEqual(page.locator("#sidebar-toggle:visible, #sidebar-menu:visible").count(), 1)
        page.locator("#sidebar-pin").click()
        self.wait_collapsed(page, False)
        self.wait_peek(page, False)
        self.assertEqual(page.locator("#sidebar-toggle:visible, #sidebar-menu:visible").count(), 1)
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-sidebar-v1')"), "expanded")
        page.reload()
        page.locator("#case-search").wait_for()
        self.wait_collapsed(page, False)
        self.assertTrue(page.locator("#workbench-sidebar").is_visible())
        self.assertAlmostEqual(page.locator("main").bounding_box()["x"], expanded["x"], delta=1)

    def test_medium_screen_defaults_to_collapsed_and_honors_an_explicit_preference(self):
        page = self.open_page(width=820)
        page.locator("#case-search").wait_for()
        self.wait_collapsed(page)
        self.assertTrue(page.locator("#sidebar-rail").is_visible())
        self.assert_no_overflow(page)
        self.rail_button(page, "library-nav").hover()
        self.wait_peek(page)
        page.locator("#sidebar-pin").click()
        self.wait_collapsed(page, False)
        page.reload()
        page.locator("#case-search").wait_for()
        self.wait_collapsed(page, False)
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-sidebar-v1')"), "expanded")
        self.assert_no_overflow(page)

    def test_hover_opens_from_rail_padding_row_gaps_and_blank_space(self):
        page = self.open_page(preference="collapsed")
        page.locator("#case-search").wait_for()
        before_hash = page.evaluate("location.hash")
        before_main = page.locator("main").bounding_box()
        rail = page.locator("#sidebar-rail").bounding_box()
        home = self.rail_button(page, "home-nav").bounding_box()
        reviews = self.rail_button(page, "reviews-nav").bounding_box()
        workflow = page.locator("#sidebar-rail nav").bounding_box()
        tools = page.locator(".sidebar-rail-tools").bounding_box()
        points = {
            "left_padding": (rail["x"] + 2, reviews["y"] + reviews["height"] / 2),
            "right_padding": (rail["x"] + rail["width"] - 2, reviews["y"] + reviews["height"] / 2),
            "row_gap": (home["x"] + home["width"] / 2, (home["y"] + home["height"] + reviews["y"]) / 2),
            "blank_space": (rail["x"] + rail["width"] / 2, (workflow["y"] + workflow["height"] + tools["y"]) / 2),
        }
        for name, point in points.items():
            with self.subTest(hover_region=name):
                page.mouse.move(1200, 100)
                self.wait_peek(page, False)
                page.mouse.move(*point)
                self.wait_peek(page)
                self.assertEqual(page.evaluate("location.hash"), before_hash)
                self.assertEqual(page.locator("main").bounding_box(), before_main)

        # Moving off a button into the adjacent rail gap must keep its pending opening.
        page.mouse.move(1200, 100)
        self.wait_peek(page, False)
        page.mouse.move(home["x"] + home["width"] / 2, home["y"] + home["height"] / 2)
        page.mouse.move(*points["row_gap"])
        self.wait_peek(page)
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-sidebar-v1')"), "collapsed")

    def test_menu_click_toggles_the_popup_and_keeps_it_closed_until_reopened(self):
        page = self.open_page(preference="collapsed")
        page.locator("#case-search").wait_for()
        menu = page.locator("#sidebar-menu")
        self.assertEqual(menu.get_attribute("aria-label"), "打开边栏")
        menu.click()
        self.wait_peek(page)
        self.assertEqual(menu.get_attribute("aria-label"), "收起边栏")
        self.assertEqual(menu.get_attribute("aria-expanded"), "true")
        menu.click()
        self.wait_peek(page, False)
        page.wait_for_timeout(300)
        self.assertFalse(page.locator("html").evaluate("node => node.classList.contains('sidebar-peek')"))
        self.assertEqual(menu.get_attribute("aria-label"), "打开边栏")
        self.assertEqual(menu.get_attribute("aria-expanded"), "false")
        self.assertEqual(page.evaluate("document.activeElement.id"), "sidebar-menu")
        menu.click()
        self.wait_peek(page)
        self.assertEqual(page.locator("#sidebar-toggle:visible, #sidebar-menu:visible").count(), 1)
        self.assertEqual(page.locator("#sidebar-pin svg").count(), 0)
        page.mouse.move(1200, 100)
        self.wait_peek(page, False)
        self.wait_collapsed(page)

    def test_hover_transfers_into_the_popup_without_navigation_or_content_reflow(self):
        page = self.open_page(preference="collapsed")
        page.locator("#case-search").wait_for()
        before_hash = page.evaluate("location.hash")
        before_main = page.locator("main").bounding_box()
        trigger = self.rail_button(page, "reviews-nav")
        page.mouse.move(1200, 100)
        # A quick pass through the icon rail must cancel pending opening.
        trigger_box = trigger.bounding_box()
        page.mouse.move(trigger_box["x"] + trigger_box["width"] / 2,
                        trigger_box["y"] + trigger_box["height"] / 2)
        page.mouse.move(1200, 100)
        page.wait_for_timeout(300)
        self.assertFalse(page.locator("html").evaluate("node => node.classList.contains('sidebar-peek')"))
        self.assertTrue(page.locator("#workbench-sidebar").is_hidden())
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        # A deliberate, stable hover still reveals the full panel.
        trigger.hover()
        self.wait_peek(page)
        self.assertTrue(page.locator("#workbench-sidebar").is_visible())
        page.wait_for_function("() => getComputedStyle(document.querySelector('#workbench-sidebar')).opacity === '1'")
        rail = page.locator("#sidebar-rail").bounding_box()
        popup = page.locator("#workbench-sidebar").bounding_box()
        target_box = page.locator("#reviews-nav").bounding_box()
        self.assertGreaterEqual(popup["x"], rail["x"] + rail["width"] - 1)
        # Give the user time to cross the small corridor between icon and panel.
        trigger_box = trigger.bounding_box()
        gap_x = (rail["x"] + rail["width"] + popup["x"]) / 2
        page.mouse.move(gap_x, trigger_box["y"] + trigger_box["height"] / 2)
        page.wait_for_timeout(175)
        self.assertTrue(page.locator("html").evaluate("node => node.classList.contains('sidebar-peek')"))
        page.mouse.move(target_box["x"] + target_box["width"] / 2,
                        target_box["y"] + target_box["height"] / 2)
        page.wait_for_timeout(350)
        self.wait_peek(page)
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        self.assertTrue(page.locator("#material-basket-dialog").is_hidden())
        after_main = page.locator("main").bounding_box()
        for dimension in ("x", "width"):
            self.assertAlmostEqual(after_main[dimension], before_main[dimension], delta=1)
        self.assert_no_overflow(page)
        # Leave both the rail and the popup; the grace period then expires.
        page.mouse.move(1200, 100)
        self.wait_peek(page, False)
        self.assertTrue(page.locator("#workbench-sidebar").is_hidden())
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-sidebar-v1')"), "collapsed")

    def test_popup_aligns_icons_on_tall_and_short_screens_and_distinguishes_active_from_hover(self):
        page = self.open_page(preference="collapsed")
        page.locator("#case-search").wait_for()
        before_hash = page.evaluate("location.hash")
        self.rail_button(page, "reviews-nav").hover()
        self.wait_peek(page)
        popup = page.locator("#workbench-sidebar")
        bounds = popup.bounding_box()
        # The panel shares the rail's row positions, including the bottom tools.
        self.assertLessEqual(bounds["height"], 884)
        self.assertGreaterEqual(bounds["y"], 0)
        self.assertLessEqual(bounds["y"] + bounds["height"], 900)
        self.assertEqual(page.locator('#workbench-sidebar .brand, .sidebar-rail .brand-mark').count(), 0)
        self.assert_sidebar_icons_aligned(page)
        active = page.locator("#library-nav")
        active_style = active.evaluate("node => ({background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color})")
        preview = page.locator("#reviews-nav")
        preview.hover()
        preview_style = preview.evaluate("node => ({background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color})")
        self.assertNotEqual(active_style["background"], preview_style["background"])
        self.assertNotEqual(active_style["color"], preview_style["color"])
        self.assertNotEqual(preview_style["background"], "rgba(0, 0, 0, 0)")
        self.assertEqual(active.evaluate("node => getComputedStyle(node).backgroundColor"), active_style["background"])
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        self.assert_route(page, "library")

        page.set_viewport_size({"width": 1280, "height": 720})
        self.wait_peek(page)
        self.assert_sidebar_icons_aligned(page)
        page.set_viewport_size({"width": 1280, "height": 420})
        self.wait_peek(page)
        bounds = popup.bounding_box()
        self.assertLessEqual(bounds["height"], 404)
        self.assertLessEqual(bounds["y"] + bounds["height"], 420)
        self.assertTrue(popup.evaluate("node => node.scrollHeight > node.clientHeight"))
        self.assert_sidebar_icons_aligned(page, visible_only=True)
        page.locator("#connection-status > summary").click()
        for selector in ("#files-nav", "#sync-button"):
            with self.subTest(short_screen_control=selector):
                control = page.locator(selector)
                control.scroll_into_view_if_needed()
                control.focus()
                visible = control.bounding_box()
                self.assertGreaterEqual(visible["y"], bounds["y"])
                self.assertLessEqual(visible["y"] + visible["height"], bounds["y"] + bounds["height"])
                self.assertEqual(page.evaluate("document.activeElement.id"), selector[1:])
                self.assert_sidebar_icons_aligned(page, visible_only=True)
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        self.assert_no_overflow(page)

    def test_sync_menu_reveals_connection_failure_and_supports_retry(self):
        page = self.open_page()
        page.locator("#case-search").wait_for()
        status = page.locator("#connection-status")
        self.assertFalse(status.evaluate("node => node.open"))
        self.assertTrue(page.locator("#sync-button").is_hidden())
        status.locator("summary").click()
        self.assertTrue(page.locator("#sync-button").is_visible())
        page.locator("#sync-button").focus()
        page.keyboard.press("Escape")
        self.assertFalse(status.evaluate("node => node.open"))
        self.assert_route(page, "library")

        state_calls, sync_calls = [], []

        def state_read(route):
            state_calls.append(route.request.url)
            if len(state_calls) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "temporary connection failure"}))
            else:
                route.continue_()

        def sync(route):
            sync_calls.append(route.request.url)
            if len(sync_calls) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "temporary sync failure"}))
            else:
                route.continue_()

        page.route("**/api/state*", state_read)
        page.route("**/api/sync", sync)
        page.evaluate("refresh()")
        page.locator("#connection-status[open] #sync-button").wait_for()
        self.assertEqual(page.locator("#sync-label").inner_text(), "重试同步")
        page.locator("#sync-button").click()
        page.locator("#sync-detail").get_by_text("temporary sync failure", exact=False).wait_for()
        self.assertTrue(status.evaluate("node => node.open"))
        self.assertEqual(page.locator("#sync-label").inner_text(), "重试同步")
        page.locator("#sync-button").click()
        page.wait_for_function("() => !document.querySelector('#connection-status').open && !document.querySelector('#sync-button').disabled")
        self.assertEqual(len(sync_calls), 2)
        self.assertEqual(page.locator("#connection-text").inner_text(), "案例库同步")
        self.assertTrue(page.locator("#error-banner").is_hidden())
        self.assertTrue(page.locator("#sync-button").is_hidden())

    def test_content_subpages_keep_the_content_creation_module_selected(self):
        page = self.open_page("#/202H/ai")
        page.locator("#ai-task-list").wait_for()
        for route, ready in (("scripts", "#content-flow-query"), ("production", "#content-flow-query")):
            with self.subTest(subpage=route):
                page.goto(f"{self.base}/#/202H/{route}")
                page.locator(ready).wait_for()
                self.assert_route(page, "content")
                self.assertEqual(page.locator(".workflow-nav .nav-item.active").inner_text().strip(), "内容创作")
                self.assertEqual(page.locator("#ai-nav").get_attribute("aria-current"), "page")
                page.locator("#sidebar-toggle").click()
                self.wait_collapsed(page)
                self.assertEqual(self.rail_button(page, "ai-nav").get_attribute("aria-current"), "page")
                self.rail_button(page, "ai-nav").click()
                page.locator("#content-flow-list").wait_for()
                self.assert_route(page, "content")
                page.mouse.move(1200, 100)
                self.wait_peek(page, False)
                self.rail_button(page, "ai-nav").hover()
                self.wait_peek(page)
                page.locator("#sidebar-pin").click()
                self.wait_collapsed(page, False)

    def test_rail_clicks_open_the_original_modules_and_material_basket(self):
        reviews = self.request("GET", "/api/reviews")[2]["reviews"]
        quote = next(item for item in reviews if item["text"] == "That’s a lot of wobble")
        self.server.app.reviews.set_pick(quote["id"], True)
        page = self.open_page(preference="collapsed")
        for target, name, ready in [
            ("reviews-nav", "reviews", "#rev-page"),
            ("files-nav", "settings", ".settings-page"),
            ("library-nav", "library", "#case-search"),
        ]:
            with self.subTest(module=target):
                self.rail_button(page, target).click()
                page.locator(ready).wait_for()
                self.assert_route(page, name)
                self.wait_collapsed(page)
        before_hash = page.evaluate("location.hash")
        self.rail_button(page, "materials-nav").click()
        basket = page.locator("#material-basket-dialog[open]")
        basket.get_by_text(quote["text"], exact=True).wait_for()
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        basket.locator("[data-material-close]").click()
        self.assertTrue(page.locator("#material-basket-dialog").is_hidden())
        self.assertEqual(page.evaluate("location.hash"), before_hash)
        self.wait_collapsed(page)

    def test_touch_menu_and_escape_restore_focus_to_the_rail_trigger(self):
        page = self.open_page(preference="collapsed", touch=True)
        page.locator("#case-search").wait_for()
        page.locator("#sidebar-menu").tap()
        self.wait_peek(page)
        page.locator("#library-nav").focus()
        page.keyboard.press("Escape")
        self.wait_peek(page, False)
        self.assertEqual(page.evaluate("document.activeElement.id"), "sidebar-menu")
        self.wait_collapsed(page)
        trigger = self.rail_button(page, "reviews-nav")
        trigger.hover()
        self.wait_peek(page)
        page.locator("#reviews-nav").hover()
        page.locator("#reviews-nav").focus()
        page.keyboard.press("Escape")
        self.wait_peek(page, False)
        self.assertEqual(page.evaluate("document.activeElement.dataset.sidebarTarget"), "reviews-nav")
        self.assert_route(page, "library")
        for route, name, ready, tail in [
            ("#/202H/settings", "settings", ".settings-page", ""),
            (f"#case/{self.identifier}", "case", ".detail-heading", "/" + self.identifier),
        ]:
            with self.subTest(escape_on=name):
                page.goto(self.base + "/" + route)
                page.locator(ready).wait_for()
                self.assert_route(page, name, tail=tail)
                before_hash = page.evaluate("location.hash")
                trigger.hover()
                self.wait_peek(page)
                page.locator("#reviews-nav").hover()
                page.locator("#reviews-nav").focus()
                page.keyboard.press("Escape")
                self.wait_peek(page, False)
                self.assertEqual(page.evaluate("location.hash"), before_hash)
                self.assert_route(page, name, tail=tail)
                self.assertEqual(page.evaluate("document.activeElement.dataset.sidebarTarget"), "reviews-nav")

    def test_product_selection_keyboard_focus_and_async_rail_updates(self):
        page = self.open_page(preference="collapsed")
        page.locator("#case-search").wait_for()
        status, _, data = self.request("POST", "/api/products/catalog", {"action": "add", "product": "129N"})
        self.assertEqual(status, 200, data)
        page.evaluate("WorkbenchShell.refreshProducts()")
        self.rail_button(page, "reviews-nav").hover()
        self.wait_peek(page)
        picker = page.locator("#global-product")
        picker.focus()
        picker.select_option("129N")
        self.assert_route(page, "library", product="129N")
        self.assertEqual(page.evaluate("document.activeElement.id"), "global-product")
        # A closed native picker may retain focus after a mouse selection;
        # that stale focus must not keep the hovering panel open.
        page.mouse.move(1200, 100)
        self.wait_peek(page, False)
        self.assert_route(page, "library", product="129N")

        # Enter the rail with a real Tab, then continue into the full panel.
        page.evaluate("() => { document.body.tabIndex = -1; document.body.focus(); }")
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate("document.activeElement.id"), "sidebar-menu")
        self.wait_peek(page)
        for _ in range(12):
            page.keyboard.press("Tab")
            if page.evaluate("document.querySelector('#workbench-sidebar').contains(document.activeElement)"):
                break
        self.assertTrue(page.evaluate("document.querySelector('#workbench-sidebar').contains(document.activeElement)"))
        # Hovering out must preserve the panel while keyboard navigation is in it.
        page.locator("#workbench-sidebar .sidebar-heading").hover()
        page.mouse.move(1200, 100)
        page.wait_for_timeout(200)
        self.wait_peek(page)
        self.assertTrue(page.evaluate("document.querySelector('#workbench-sidebar').contains(document.activeElement)"))

        # The originals remain the authority for async counts and availability.
        page.evaluate("""() => {
          const materials = document.querySelector('#materials-nav .nav-count');
          materials.textContent = '7'; materials.hidden = false;
          const ai = document.querySelector('#ai-active-count');
          ai.textContent = '3'; ai.hidden = false;
          document.querySelector('#files-nav').hidden = true;
        }""")
        material_badge = self.rail_button(page, "materials-nav").locator("[data-sidebar-count]")
        ai_badge = self.rail_button(page, "ai-nav").locator("[data-sidebar-count]")
        page.wait_for_function("""() =>
          document.querySelector('[data-sidebar-target="materials-nav"] [data-sidebar-count]').textContent === '7' &&
          document.querySelector('[data-sidebar-target="ai-nav"] [data-sidebar-count]').textContent === '3' &&
          document.querySelector('[data-sidebar-target="files-nav"]').hidden
        """)
        self.assertTrue(material_badge.is_visible())
        self.assertTrue(ai_badge.is_visible())
        self.assertTrue(self.rail_button(page, "files-nav").is_hidden())
        page.evaluate("""() => {
          document.querySelector('#materials-nav .nav-count').textContent = '0';
          document.querySelector('#ai-active-count').hidden = true;
          document.querySelector('#files-nav').hidden = false;
        }""")
        page.wait_for_function("""() =>
          document.querySelector('[data-sidebar-target="materials-nav"] [data-sidebar-count]').hidden &&
          document.querySelector('[data-sidebar-target="ai-nav"] [data-sidebar-count]').hidden &&
          !document.querySelector('[data-sidebar-target="files-nav"]').hidden
        """)
        self.assertEqual(material_badge.inner_text(), "0")
        self.assertTrue(material_badge.is_hidden())
        self.assertTrue(ai_badge.is_hidden())
        self.assertTrue(self.rail_button(page, "files-nav").is_visible())
        # Switching back to the mouse clears keyboard mode, even if the
        # product select is then left focused after changing its value.
        page.locator("#workbench-sidebar .sidebar-heading").click(position={"x": 1, "y": 1})
        picker.focus()
        picker.select_option("202H")
        self.assert_route(page, "library")
        page.mouse.move(1200, 100)
        self.wait_peek(page, False)
        self.assert_route(page, "library")

    def test_mobile_tabs_and_product_picker_survive_a_saved_collapsed_preference(self):
        page = self.open_page(width=390, preference="collapsed", touch=True)
        page.locator("#case-search").wait_for()
        self.assertTrue(page.locator("#sidebar-toggle").is_hidden())
        self.assertTrue(page.locator("#sidebar-rail").is_hidden())
        self.assertTrue(page.locator("#global-product").is_visible())
        self.assert_no_overflow(page)
        page.locator("#reviews-nav").tap()
        page.locator("#rev-page").wait_for()
        self.assert_route(page, "reviews")
        tab = page.locator("#reviews-nav").bounding_box()
        self.assertGreater(tab["y"], 800)
        status, _, data = self.request("POST", "/api/products/catalog", {"action": "add", "product": "129N"})
        self.assertEqual(status, 200, data)
        page.evaluate("WorkbenchShell.refreshProducts()")
        page.locator("#global-product").select_option("129N")
        self.assert_route(page, "reviews", product="129N")
        page.locator("#library-nav").tap()
        page.locator("#case-search").wait_for()
        self.assert_route(page, "library", product="129N")
        page.reload()
        page.locator("#case-search").wait_for()
        self.assert_route(page, "library", product="129N")
        self.assertTrue(page.locator("#sidebar-toggle").is_hidden())
        self.assertTrue(page.locator("#sidebar-rail").is_hidden())
        self.assertTrue(page.locator("#global-product").is_visible())
        self.assertTrue(page.locator("#library-nav").is_visible())
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-sidebar-v1')"), "collapsed")
        self.assert_no_overflow(page)


if __name__ == "__main__":
    unittest.main()
