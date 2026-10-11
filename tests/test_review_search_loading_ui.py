"""Review search scheduling checks with isolated data and deterministic debounce timers."""
import json
import unittest
from urllib.parse import parse_qs, urlparse

import test_review_ui as review


class ReviewSearchLoadingTests(unittest.TestCase):
    setUpClass = classmethod(review.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review.ReviewPageTests.tearDownClass.__func__)
    setUp = review.ReviewPageTests.setUp
    stop_server = review.ReviewPageTests.stop_server
    request = review.ReviewPageTests.request

    def page(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.add_init_script("""(() => {
          window.reviewSearchReads = [];
          window.reviewSearchFinished = [];
          const original = window.fetch;
          window.fetch = function(input, options) {
            const path = String(input), match = path.startsWith('/api/reviews?');
            const query = match ? new URL(path, location.href).searchParams.get('q') || '' : '';
            if (match) window.reviewSearchReads.push({path,query});
            return original.call(this, input, options).then(async response => {
              if (match) {await response.clone().json();window.reviewSearchFinished.push(query);}
              return response;
            });
          };
        })();""")
        page.goto(f"{self.base}/#/202H/reviews")
        page.locator(".rev-card").first.wait_for()
        page.evaluate("""() => {
          const realSet = window.setTimeout.bind(window), realClear = window.clearTimeout.bind(window);
          let next = 0;
          window.searchTimers = new Map();
          window.searchDelays = [];
          window.setTimeout = (callback, delay, ...args) => {
            if (delay !== 150) return realSet(callback, delay, ...args);
            const id = --next;
            searchTimers.set(id, () => callback(...args));
            searchDelays.push(delay);
            return id;
          };
          window.clearTimeout = id => searchTimers.has(id) ? searchTimers.delete(id) : realClear(id);
          window.flushSearchTimers = () => {
            const tasks = [...searchTimers.values()];searchTimers.clear();
            tasks.forEach(task => task());
          };
        }""")
        return page

    def reads(self, page, query):
        return page.evaluate("query => reviewSearchReads.filter(read => read.query === query).length", query)

    def applied(self, page, query):
        page.locator("#rev-active").get_by_text(f"“{query}”", exact=False).wait_for()

    def settle(self, page):
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def hold_query(self, page, query, held):
        def pause(route):
            actual = parse_qs(urlparse(route.request.url).query).get("q", [""])[0]
            if actual == query:
                held.append(route)
                route.request.frame.evaluate("window.heldSearchReady = true")
            else:
                route.continue_()
        page.route("**/api/reviews?**", pause)

    def test_150ms_debounce_enter_flush_and_repeated_enter_does_not_repeat_search(self):
        page = self.page()
        page.locator("#rev-q").fill("clamp")
        self.assertEqual(page.evaluate("searchTimers.size"), 1)
        self.assertEqual(page.evaluate("searchDelays"), [150])
        self.assertEqual(self.reads(page, "clamp"), 0)
        page.locator("#rev-q").press("Enter")
        self.applied(page, "clamp")
        self.assertEqual(page.evaluate("searchTimers.size"), 0)
        page.locator("#rev-q").press("Enter")
        page.locator("#rev-q").press("Enter")
        page.evaluate("flushSearchTimers()")
        self.assertEqual(self.reads(page, "clamp"), 1)

    def test_ime_intermediate_text_and_confirmation_enter_do_not_search(self):
        page = self.page()
        page.evaluate("""() => {
          const field = document.querySelector('#rev-q');
          field.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
          field.value = '晃';
          field.dispatchEvent(new InputEvent('input', {bubbles:true,isComposing:true}));
          field.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',keyCode:229,isComposing:true,bubbles:true}));
          flushSearchTimers();
        }""")
        self.assertEqual(self.reads(page, "晃"), 0)
        self.assertEqual(page.evaluate("searchTimers.size"), 0)
        page.evaluate("""() => {
          const field = document.querySelector('#rev-q');
          field.value = '晃动';
          field.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true,data:'晃动'}));
          field.dispatchEvent(new InputEvent('input', {bubbles:true,isComposing:false}));
          field.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',keyCode:229,bubbles:true}));
        }""")
        self.assertEqual(page.evaluate("searchTimers.size"), 1)
        self.assertEqual(self.reads(page, "晃动"), 0)
        page.evaluate("flushSearchTimers()")
        self.applied(page, "晃动")
        page.locator("#rev-query-note").get_by_text("「晃动」也按", exact=False).wait_for()
        self.assertIn("wobbl", page.locator("#rev-query-note").inner_text())
        self.assertEqual(self.reads(page, "晃动"), 1)

    def test_old_success_and_old_failure_cannot_paint_during_next_debounce(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                page, held = self.page(), []
                before = page.locator("#rev-results").inner_html()
                self.hold_query(page, "clamp", held)
                page.locator("#rev-q").fill("clamp")
                page.evaluate("flushSearchTimers()")
                page.wait_for_function("() => window.heldSearchReady")
                page.locator("#rev-q").fill("swords")
                if failure:
                    held.pop().fulfill(status=503, content_type="application/json", body=json.dumps({"error": "stale search failure"}))
                else:
                    held.pop().continue_()
                page.wait_for_function("() => reviewSearchFinished.includes('clamp')")
                self.settle(page)
                self.assertEqual(page.locator("#rev-results").inner_html(), before)
                self.assertEqual(page.locator("#rev-active").inner_text(), "")
                page.evaluate("flushSearchTimers()")
                self.applied(page, "swords")
                self.assertEqual(self.reads(page, "swords"), 1)

    def test_returning_to_same_pending_query_accepts_shared_result(self):
        page, held = self.page(), []
        self.hold_query(page, "clamp", held)
        page.locator("#rev-q").fill("clamp")
        page.evaluate("flushSearchTimers()")
        page.wait_for_function("() => window.heldSearchReady")
        page.locator("#rev-q").fill("swords")
        page.locator("#rev-q").fill("clamp")
        page.evaluate("flushSearchTimers()")
        held.pop().continue_()
        self.applied(page, "clamp")
        self.assertEqual(self.reads(page, "clamp"), 1)
        self.assertEqual(self.reads(page, "swords"), 0)

    def test_filter_and_reset_cancel_search_timer_and_use_latest_text_once(self):
        page = self.page()
        page.locator("#rev-q").fill("clamp")
        page.locator('[data-quick="questions"]').click()
        self.applied(page, "clamp")
        self.assertEqual(page.evaluate("searchTimers.size"), 0)
        page.evaluate("flushSearchTimers()")
        self.assertEqual(self.reads(page, "clamp"), 1)
        path = page.evaluate("reviewSearchReads.find(read => read.query === 'clamp').path")
        self.assertIn("replied=no", path)
        page.locator("#rev-q").fill("swords")
        page.locator("#rev-page [data-reset]").click()
        page.locator("#rev-count").get_by_text("10 条", exact=False).wait_for()
        page.evaluate("flushSearchTimers()")
        self.assertEqual(self.reads(page, "swords"), 0)
        self.assertEqual(page.evaluate("searchTimers.size"), 0)

    def test_leaving_or_hash_redraw_cancels_old_field_timer(self):
        for next_hash in ("#/202H/library", "#/202H/reviews?topic=stability"):
            with self.subTest(next_hash=next_hash):
                page = self.page()
                page.locator("#rev-q").fill("clamp")
                page.evaluate("hash => {location.hash = hash;}", next_hash)
                if "reviews" in next_hash:
                    page.locator(".rev-card").get_by_text("wobble", exact=False).wait_for()
                else:
                    page.locator("#case-results").wait_for()
                page.evaluate("flushSearchTimers()")
                self.assertEqual(self.reads(page, "clamp"), 0)
                self.assertEqual(page.evaluate("searchTimers.size"), 0)

    def test_background_route_does_not_discard_unsubmitted_text(self):
        page = self.page()
        page.locator("#rev-q").fill("clamp")
        page.evaluate("CaseReviews.route()")
        self.assertEqual(page.locator("#rev-q").input_value(), "clamp")
        self.assertEqual(page.evaluate("searchTimers.size"), 1)
        page.evaluate("flushSearchTimers()")
        self.applied(page, "clamp")
        self.assertEqual(self.reads(page, "clamp"), 1)

    def test_global_refresh_discards_old_cards_and_same_params_still_refresh(self):
        page, held = self.page(), []
        before = page.locator("#rev-results").inner_html()
        self.hold_query(page, "", held)
        page.evaluate("window.dispatchEvent(new CustomEvent('workbench:basketchange',{detail:{source:'global'}}))")
        page.wait_for_function("() => window.heldSearchReady")
        page.locator("#rev-q").fill("clamp")
        held.pop().continue_()
        page.wait_for_function("() => reviewSearchFinished.filter(query => query === '').length === 2")
        self.settle(page)
        self.assertEqual(page.locator("#rev-results").inner_html(), before)
        self.assertEqual(page.locator("#rev-active").inner_text(), "")
        page.evaluate("flushSearchTimers()")
        self.applied(page, "clamp")
        page.evaluate("window.dispatchEvent(new CustomEvent('workbench:basketchange',{detail:{source:'global'}}))")
        page.wait_for_function("() => reviewSearchFinished.filter(query => query === 'clamp').length === 2")
        self.assertEqual(self.reads(page, "clamp"), 2)

    def test_same_trimmed_text_does_not_discard_a_pending_record_refresh(self):
        page, held = self.page(), []
        page.locator("#rev-q").fill("clamp")
        page.evaluate("flushSearchTimers()")
        self.applied(page, "clamp")
        self.hold_query(page, "clamp", held)
        page.evaluate("window.dispatchEvent(new CustomEvent('workbench:basketchange',{detail:{source:'global'}}))")
        page.wait_for_function("() => window.heldSearchReady")
        _, _, result = self.request("GET", "/api/reviews?product=202H&q=clamp&sort=likes&limit=50")
        result["reviews"][0]["text"] = "clamp latest record"
        page.locator("#rev-q").fill("  clamp  ")
        page.evaluate("flushSearchTimers()")
        held.pop().fulfill(status=200, content_type="application/json", body=json.dumps(result))
        page.locator(".rev-text").get_by_text("latest record", exact=False).wait_for()
        self.assertEqual(self.reads(page, "clamp"), 2)


if __name__ == "__main__":
    unittest.main()
