"""Plugin comments retain their source identity through live views and quotes."""
from __future__ import annotations

import unittest

import test_review_ui as library
from test_performance import LINKED
from review_store import review_id


PARENT = "7664986913497009101"
REPLY = "7664986913497009102"
LATER = "7664986913497009103"


def comment(identifier=PARENT, **fields):
    return {"id": identifier, "videoId": LINKED, "text": "Does this fit a short blade?",
            "likes": 20, "authorName": "Blade Friend", "authorHandle": "blade.friend",
            "createdAt": "2026-10-25T08:00:00Z", **fields}


class ReviewAuthorPageTests(unittest.TestCase):
    setUp = library.ReviewPageTests.setUp
    stop_server = library.ReviewPageTests.stop_server
    request = library.ReviewPageTests.request
    open_page = library.ReviewPageTests.open_page
    card = library.ReviewPageTests.card
    count = library.ReviewPageTests.count

    @classmethod
    def setUpClass(cls):
        library.ReviewPageTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        library.ReviewPageTests.tearDownClass.__func__(cls)

    def push(self, *comments):
        if not hasattr(self, "key"):
            self.key = self.request("GET", "/api/public-stats/link")[2]["key"]
        status, _, data = self.request("POST", "/api/public-stats/comments",
            {"videos": [{"id": comments[0]["videoId"], "author": "bushcraftedge", "comments": list(comments)}]},
            headers={"X-TKL-Key": self.key, "Origin": "chrome-extension://abcdefghijklmnop"}, authenticated=False)
        self.assertEqual(status, 200, data)
        return data

    def test_push_updates_names_open_threads_and_quotes_without_duplicates(self):
        page = self.open_page(route=f"#reviews?video={LINKED}")
        self.count(page, "3 条")
        unsafe_name = 'Blade Friend <img src=x onerror="window.authorInjected=true">'
        parent = comment(authorName=unsafe_name)
        reply = comment(REPLY, text="Yes, this is the blade I use.", authorName="Helpful Viewer",
                        authorHandle="helpful.viewer", parentId=PARENT, likes=2)
        self.push(parent, reply)
        page.evaluate("() => refresh(true)")  # Same state poll the open workbench uses.
        self.count(page, "5 条")
        card = self.card(page, parent["text"])
        self.assertEqual(card.locator(".rev-head [data-review-author]").inner_text(), "Blade Friend · @blade.friend")
        self.assertEqual(card.locator("[data-review-author] img").count(), 0)
        self.assertIsNone(page.evaluate("window.authorInjected"))
        card.locator("[data-thread]").click()
        card.locator(".rev-thread .rev-reply").wait_for()
        self.assertEqual(card.locator(".rev-thread [data-review-author]").inner_text(), "Helpful Viewer · @helpful.viewer")

        newer = comment(LATER, text="Mine fits too.", authorName="", authorHandle="@only.handle", parentId=PARENT, likes=0)
        parent = comment(authorName="Updated Viewer")
        self.push(parent, newer)
        self.push(parent, newer)  # Replaying the plugin's same batch must not add cards.
        page.evaluate("() => refresh(true)")
        self.count(page, "6 条")
        card.locator(".rev-thread .rev-reply").nth(1).wait_for()
        self.assertEqual(card.locator("[data-thread]").get_attribute("aria-expanded"), "true")
        self.assertEqual(card.locator(".rev-thread .rev-reply").count(), 2)
        self.assertIn("@only.handle", card.locator(".rev-thread").inner_text())
        self.assertEqual(page.locator("#rev-video").input_value(), LINKED)
        card.get_by_role("button", name="复制引用", exact=True).click()
        page.wait_for_function("() => navigator.clipboard.readText().then(text => text.includes('Updated Viewer · @blade.friend'))")
        page.locator("#rev-copy-all").click()
        page.wait_for_function("() => navigator.clipboard.readText().then(text => text.includes('用户声音摘录') && text.includes('@only.handle'))")

        unknown = comment("7664986913497009104", text="An unnamed comment.", authorName="", authorHandle="")
        self.push(unknown)
        page.evaluate("() => refresh(true)")
        self.card(page, unknown["text"]).wait_for()
        self.assertEqual(self.card(page, unknown["text"]).locator("[data-review-author]").inner_text(), "评论者未记录")
        self.assertNotIn("不含评论者姓名", page.locator(".rev-heading").inner_text())

    def test_author_updates_keep_the_focused_material_note_and_copy_identity(self):
        self.push(comment())
        page = self.open_page(route=f"#reviews?video={LINKED}")
        self.card(page, "Does this fit a short blade?").get_by_role("button", name="摘录", exact=True).click()
        self.card(page, "Does this fit a short blade?").get_by_role("button", name="已摘录", exact=True).wait_for()
        page.locator("#rev-basket-toggle").click()
        basket = page.locator("#material-basket-dialog")
        field = basket.locator("[data-material-note]")
        field.fill("保留正在输入的备注，不要替换")
        field.evaluate("element => { window.originalNoteField = element; element.setSelectionRange(3, 8); }")
        self.push(comment(authorName="Updated Viewer"))
        page.evaluate("() => refresh(true)")
        # Await the independent material refresh too, without moving focus.
        page.evaluate("() => MaterialBasket.refresh(true)")
        self.assertEqual(field.input_value(), "保留正在输入的备注，不要替换")
        self.assertTrue(field.evaluate("element => element === window.originalNoteField && element === document.activeElement"))
        self.assertEqual(field.evaluate("element => [element.selectionStart, element.selectionEnd]"), [3, 8])
        basket.locator("[data-material-save]").click()
        page.locator("#toast:has-text('备注已保存')").wait_for()
        basket.locator("[data-material-author]:has-text('Updated Viewer')").wait_for()
        material = self.request("GET", "/api/materials")[2]["items"][0]
        self.assertEqual((material["author_name"], material["author_handle"]), ("Updated Viewer", "blade.friend"))
        self.assertEqual(material["note"], "保留正在输入的备注，不要替换")
        basket.locator("[data-material-copy]").click()
        page.wait_for_function("() => navigator.clipboard.readText().then(text => text.includes('Updated Viewer · @blade.friend') && text.includes('保留正在输入的备注'))")

    def test_push_during_search_composition_preserves_input_focus_and_filters(self):
        page = self.open_page(route=f"#reviews?video={LINKED}")
        field = page.locator("#rev-q")
        field.focus()
        field.evaluate("""element => {
          window.originalSearchField = element;
          element.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
          element.value = 'short';
          element.setSelectionRange(1, 3);
          element.dispatchEvent(new InputEvent('input', {bubbles:true, isComposing:true}));
        }""")
        self.push(comment())
        page.evaluate("() => refresh(true)")
        page.evaluate("() => CaseReviews.commentsChanged()")
        self.assertEqual(field.input_value(), "short")
        self.assertTrue(field.evaluate("element => element === originalSearchField && document.activeElement === element"))
        self.assertEqual(field.evaluate("element => [element.selectionStart, element.selectionEnd]"), [1, 3])
        self.assertEqual(page.locator("#rev-video").input_value(), LINKED)
        field.evaluate("element => element.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true}))")
        self.count(page, "1 条")
        self.assertIn("Blade Friend · @blade.friend", page.locator(".rev-card").inner_text())
        self.assertEqual(field.input_value(), "short")

    def test_an_old_thread_response_cannot_replace_a_new_push(self):
        self.push(comment(), comment(REPLY, text="An earlier answer.", parentId=PARENT, authorName="Earlier"))
        page = self.open_page(route=f"#reviews?video={LINKED}")
        identifier = review_id("tiktok_video", PARENT)
        page.evaluate("""identifier => {
          const original = window.fetch;
          window.threadFetches = 0;
          window.releaseThreads = [];
          window.fetch = async function(input, options) {
            const response = await original.call(this, input, options);
            if (String(input) === '/api/reviews/' + identifier && ++window.threadFetches <= 2) {
              const body = await response.text();
              return new Promise(resolve => {
                window.releaseThreads.push(() => resolve(new Response(body, {status:200, headers:{'Content-Type':'application/json'}})));
              });
            }
            return response;
          };
        }""", identifier)
        card = self.card(page, "Does this fit a short blade?")
        card.locator("[data-thread]").click()
        page.wait_for_function("() => releaseThreads.length === 1")
        self.push(comment(LATER, text="A fresh answer.", parentId=PARENT, authorName="New Reader"))
        page.evaluate("() => { window.liveCommentsRefresh = CaseReviews.commentsChanged(); }")
        page.wait_for_function("() => releaseThreads.length === 2")
        page.evaluate("() => releaseThreads[0]()")
        # Repaint while the new conversation request is still pending. The old
        # request's finally must not remove that new promise and start a third.
        card.evaluate("element => window.cardBeforeSort = element")
        page.select_option("#rev-sort", "newest")
        page.wait_for_function("() => !cardBeforeSort.isConnected")
        self.assertEqual(page.evaluate("threadFetches"), 2)
        page.evaluate("() => releaseThreads[1]()")
        page.evaluate("() => liveCommentsRefresh")
        card.locator(".rev-thread .rev-reply").nth(1).wait_for()
        self.assertEqual(card.locator(".rev-thread .rev-reply").count(), 2)
        self.assertIn("New Reader", card.locator(".rev-thread").inner_text())

    def test_a_push_during_the_first_read_replaces_the_invalidated_load(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.add_init_script("""(() => {
          const original = window.fetch;
          window.fetch = async function(input, options) {
            const response = await original.call(this, input, options);
            if (String(input).startsWith('/api/reviews?') && !window.resultsHeld) {
              window.resultsHeld = true;
              const body = await response.text();
              return new Promise(resolve => {
                window.releaseOldResults = () => resolve(new Response(body, {status:200, headers:{'Content-Type':'application/json'}}));
              });
            }
            return response;
          };
        })();""")
        page.goto(f"{self.base}/#reviews?video={LINKED}", wait_until="domcontentloaded")
        page.wait_for_function("() => typeof releaseOldResults === 'function'")
        self.push(comment())
        page.evaluate("() => CaseReviews.commentsChanged()")
        self.count(page, "4 条")
        page.evaluate("() => releaseOldResults()")
        self.count(page, "4 条")
        self.assertIn("Blade Friend · @blade.friend", self.card(page, "Does this fit a short blade?").inner_text())

    def test_unassigned_comments_are_marked_in_the_all_products_view(self):
        unknown_video = "7000000000000000001"
        self.push(comment(videoId=unknown_video, text="Feedback from an unassigned video."))
        page = self.open_page(route=f"#/all/reviews?video={unknown_video}")
        self.count(page, "1 条")
        self.assertIn("未关联产品", page.locator(".rev-card .rev-head").inner_text())
        page.locator("#global-product").select_option("202H")
        self.count(page, "10 条")
        self.assertEqual(self.card(page, "Feedback from an unassigned video.").count(), 0)


if __name__ == "__main__":
    unittest.main()
