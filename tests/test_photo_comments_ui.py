"""选版 shows the AI's search words (removable, addable) and what they found in two places (cover sticker, optional
middle slide); the user picks one per place; the cover shows its sticker."""
import unittest

import test_photo_carousel_ui as base_ui


class PhotoCommentsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(base_ui.PhotoCarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(base_ui.PhotoCarouselBrowserTests.tearDownClass.__func__)
    setUp = base_ui.PhotoCarouselBrowserTests.setUp
    open = base_ui.PhotoCarouselBrowserTests.open
    step = base_ui.PhotoCarouselBrowserTests.step
    collage = base_ui.PhotoCarouselBrowserTests.collage

    def with_comments(self, **task):
        self.collage()
        self.page.evaluate("""task => {
          window.detail.versions = [{key:'A',writing:'qa',name_zh:'问答',buyer_zh:'新手',slides:[{title_lines:['WHY?'],body_lines:[],frame_time:0.2},{title_lines:['GET IT'],body_lines:[],frame_time:1}]}];
          const hit = (id,kind,text,matched) => ({id,kind,text,matched,username:'@zack',likes:12,same_video:true});
          window.detail.comment_ai = Object.assign({version:3,status:'done',pool:40,own:1,chosen:[],picks:[],
            keywords:{cover:[{word:'slip',zh:'打滑'},{word:'worth it',zh:'值不值'}],middle:[{word:'weeks',zh:''}]},
            matches:{cover:[hit('r1','concern','Does a thin fillet knife slip?',['slip']),hit('r3','concern','Is it worth it?',['worth it'])],
                     middle:[hit('r2','use','Two weeks in, still razor sharp',['weeks'])]},
            found:{cover:2,middle:1}},task);
          const api = window.api;
          window.api = async (path,options) => {
            if (path === '/api/reviews/translations/lookup' && window.commentTranslationHandler) return window.commentTranslationHandler(options.body);
            if (options && path.endsWith('/comments-pick')) {
              window.calls.push({path,body:structuredClone(options.body)});
              const ai = window.detail.comment_ai, all = [...ai.matches.cover,...ai.matches.middle];
              ai.picks = ai.picks.filter(p => p.slot !== options.body.slot);
              if (options.body.id) ai.picks.push({...all.find(m => m.id === options.body.id),slot:options.body.slot});
              ai.chosen = ai.picks.map(p => p.id);
              return structuredClone(window.detail);
            }
            if (options && /\\/comments(-stop|-apply|-search)?$/.test(path)) {window.calls.push({path,body:structuredClone(options.body)});return structuredClone(window.detail);}
            return api(path,options);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""", task)
        dialog = self.open()
        self.step(dialog, 'plan')
        return dialog

    def test_comment_translation_shows_under_the_comment_and_is_reused_after_picking(self):
        self.page.evaluate("""() => {
          window.translationAttempts = [];
          window.commentTranslationHandler = async body => {
            window.translationAttempts.push(body.ids);
            if (window.translationAttempts.length === 1) throw new Error('暂时不可用');
            return {items:Object.fromEntries(body.ids.map(id => [id,{zh:'薄刀会打滑吗？ <示例>',title_zh:''}])),pending:[],failed:[],error:''};
          };
        }""")
        hit = lambda id, text: {"id": id, "kind": "concern", "text": text, "matched": ["slip"], "username": "@zack", "likes": 12, "same_video": True}
        cover_id, middle_id = "a" * 16, "b" * 16
        dialog = self.with_comments(matches={"cover": [hit(cover_id, "Does a thin fillet knife slip?")],
                                             "middle": [{**hit(middle_id, "Two weeks in"), "zh": "用了两周"}]})
        card = dialog.locator('[data-photo-comment-group="cover"] .photo-comment').first
        shown = card.locator('.review-zh')
        shown.get_by_text('中文翻译没成功', exact=False).wait_for()
        self.assertEqual(card.locator('details').count(), 0)
        shown.get_by_role('button', name='重试').click()
        shown.get_by_text('薄刀会打滑吗？ <示例>').wait_for()
        self.assertEqual(shown.locator('*').count(), 0)
        # A comment that already carries its Chinese shows it without asking the server.
        dialog.locator('[data-photo-comment-group="middle"] .review-zh').get_by_text('用了两周').wait_for()
        self.assertEqual(self.page.evaluate('window.translationAttempts'), [[cover_id], [cover_id]])
        card.locator(f'[data-photo-comment-pick="{cover_id}"]').click()
        picked = dialog.locator('.photo-comment-picked .review-zh')
        picked.get_by_text('薄刀会打滑吗？ <示例>').wait_for()
        self.assertEqual(len(self.page.evaluate('window.translationAttempts')), 2)

    def test_words_search_and_one_pick_per_place(self):
        dialog = self.with_comments()
        cover = dialog.locator('[data-photo-comment-group="cover"]')
        self.assertEqual(cover.locator('.photo-comment').count(), 2)
        self.assertIn('打滑', cover.locator('.photo-keyword').first.inner_text())
        self.assertIn('命中：slip', cover.locator('.photo-comment').first.inner_text())
        self.assertIn('@zack', cover.locator('.photo-comment').first.inner_text())
        self.assertIn('评论你来挑', dialog.locator('.photo-comments h4').inner_text())
        # Words: removing one or adding one searches again with the whole list.
        cover.locator('[data-photo-keyword-del="cover"][data-word="slip"]').click()
        self.assertEqual(self.page.evaluate('window.calls')[-1], {'path':'/api/photo-sets/PHS-111111111111/comments-search',
                                                                  'body':{'keywords':{'cover':['worth it'],'middle':['weeks']}}})
        dialog.locator('[data-photo-keyword-add="middle"]').fill('bought, gift')
        dialog.locator('[data-photo-keyword-add="middle"]').press('Enter')
        self.assertEqual(self.page.evaluate('window.calls')[-1]['body'], {'keywords':{'cover':['slip','worth it'],'middle':['weeks','bought','gift']}})
        # Picks: one per place, another one swaps in, and a place can be cleared.
        cover.locator('[data-photo-comment-pick="r1"]').click()
        self.page.wait_for_function("() => window.detail.comment_ai.chosen.join() === 'r1'")
        dialog.locator('.photo-comment-picked').wait_for()
        dialog.locator('[data-photo-comment-pick="r3"]').click()
        self.page.wait_for_function("() => window.detail.comment_ai.chosen.join() === 'r3'")
        dialog.locator('[data-photo-comment-pick="r2"]').click()
        self.page.wait_for_function("() => window.detail.comment_ai.chosen.join() === 'r3,r2'")
        self.assertEqual(self.page.evaluate('window.calls')[-1], {'path':'/api/photo-sets/PHS-111111111111/comments-pick', 'body':{'slot':'middle','id':'r2'}})
        dialog.locator('[data-photo-comment-clear="middle"]').click()
        self.page.wait_for_function("() => window.detail.comment_ai.chosen.join() === 'r3'")
        dialog.locator('[data-photo-comments-apply="A"]').click()
        self.assertEqual(self.page.evaluate('window.calls')[-1], {'path':'/api/photo-sets/PHS-111111111111/comments-apply', 'body':{'key':'A'}})
        for width in (1280, 390):
            self.page.set_viewport_size({'width':width,'height':850})
            self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))

    def test_states(self):
        dialog = self.with_comments(status='empty', keywords=None, matches=None, found=None, note='评论库里没有这个产品能用的 TikTok 视频评论')
        self.assertIn('评论库里没有', dialog.locator('.photo-comments').inner_text())
        dialog.locator('[data-photo-comments]').click()
        self.assertTrue(self.page.evaluate("window.calls.some(c => c.path.endsWith('/comments'))"))
        self.page.evaluate("() => {window.detail.comment_ai = {version:3,status:'done',keywords:{cover:[{word:'zzz'}],middle:[]},matches:{cover:[],middle:[]},found:{cover:0,middle:0}};}")
        dialog.locator('[data-photo-comments]').click()
        dialog.get_by_text('没搜到评论').wait_for()
        self.assertIn('还没有关键词', dialog.locator('[data-photo-comment-group="middle"]').inner_text())

    def test_archived_hides_the_block(self):
        dialog = self.with_comments()
        self.page.evaluate("() => {window.detail.comments_archived = true;}")
        dialog.locator('[data-photo-comments]').click()
        dialog.locator('.photo-comments').wait_for(state='detached')

    def test_running(self):
        dialog = self.with_comments(status='running', progress='AI 正在想搜评论用的关键词…')
        dialog.locator('[data-photo-comments-stop]').wait_for()
        self.assertIn('关键词', dialog.locator('.photo-comments').inner_text())

    def test_cover_slide_shows_its_sticker(self):
        self.collage()
        self.page.evaluate("""() => {window.detail.work.slides[0].comment = {review_id:'r1',text:'Does a thin fillet knife slip?',zh:'',username:'@zack',cover:true};}""")
        dialog = self.open()
        quoted = dialog.locator('.photo-comment-quoted')
        quoted.wait_for()
        self.assertIn('Does a thin fillet knife slip?', quoted.inner_text())
        self.assertIn('首图上的评论贴纸', quoted.inner_text())
        self.assertIn('@zack', quoted.inner_text())
        self.assertEqual(quoted.locator('textarea,input').count(), 0)
        self.assertEqual(dialog.locator('[data-photo-comment-off]').count(), 1)


if __name__ == "__main__":
    unittest.main()
