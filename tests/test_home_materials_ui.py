"""Phase four home/basket checks with isolated libraries and browser profiles."""
import copy
import json
import unittest
from uuid import uuid4

import test_review_ui as review
import test_phase4_http as creation_fixture


class HomeMaterialUITests(unittest.TestCase):
    setUpClass = classmethod(review.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review.ReviewPageTests.tearDownClass.__func__)
    setUp = review.ReviewPageTests.setUp
    stop_server = review.ReviewPageTests.stop_server
    request = review.ReviewPageTests.request
    open_page = review.ReviewPageTests.open_page
    card = review.ReviewPageTests.card

    def test_home_focuses_on_four_work_categories_and_mobile_tabs_remain_accessible(self):
        page = self.open_page(route='#/202H/home', ready='[data-home-tab]')
        self.assertEqual(page.locator('#home-page h1').inner_text(), '工作概览')
        self.assertEqual(page.locator('.home-tabs [role=tab]').count(), 4)
        self.assertEqual(page.locator('.home-kpi, #home-weekly-section, #home-page [data-material-open]').count(), 0)
        self.assertIn('完整系统价值', page.locator('.home-case-list').inner_text())
        self.assertEqual(page.locator('[data-home-tab]').first.get_attribute('data-home-tab'), 'tasks')
        page.locator('[data-home-tab=tasks]').focus()
        page.keyboard.press('ArrowRight')
        self.assertEqual(page.locator('[data-home-tab=due]').get_attribute('aria-selected'), 'true')
        self.assertTrue(page.locator('#home-due-title-panel').is_visible())
        page.keyboard.press('End')
        self.assertEqual(page.locator('[data-home-tab=cases]').get_attribute('aria-selected'), 'true')
        page.keyboard.press('Home')
        self.assertEqual(page.locator('[data-home-tab=tasks]').get_attribute('aria-selected'), 'true')
        page.set_viewport_size({'width':390,'height':844})
        for key in ('tasks', 'due', 'plans', 'cases'):
            tab = page.locator(f'[data-home-tab={key}]')
            self.assertTrue(tab.is_visible())
            self.assertGreaterEqual(tab.bounding_box()['height'], 44)
            tab.click()
            self.assertEqual(tab.get_attribute('aria-selected'), 'true')
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'))
        self.server.app.product_catalog.change("add", "129N")
        page.evaluate("WorkbenchShell.registerProducts(['129N'])")
        page.locator('#global-product').select_option('129N')
        page.locator('.home-product').get_by_text('129N', exact=True).wait_for()
        self.assertNotIn('完整系统价值', page.locator('#home-page').inner_text())
        link = page.get_by_role('link', name='完整视频看板', exact=True)
        self.assertEqual(link.get_attribute('href'), '#/129N/videos?tab=board')
        link.click()
        page.locator('.perf-tabs').wait_for()

    def test_home_failed_work_read_can_retry_without_loading_trend_data(self):
        page = self.open_page(route='#/202H/home', ready='[data-home-tab]')
        # The frame now precedes the reads. Inject the next-read failure only
        # after the initial creation response has left the shared read pool.
        page.wait_for_function("() => document.querySelector('[data-home-tab=plans] .home-tab-count').textContent === '0'")
        trend_reads=[]
        page.on('request',lambda request: trend_reads.append(request.url) if '/api/performance/summary' in request.url or '/api/performance/analytics' in request.url else None)
        page.route('**/api/creation?*',lambda route: route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'temporarily unavailable'})))
        page.evaluate('() => WorkbenchHome.route()')
        page.locator('[data-home-tab=plans]').click()
        page.get_by_text('待写计划暂时无法读取。', exact=True).wait_for()
        self.assertEqual(page.locator('[data-home-tab=plans] .home-tab-count').inner_text(), '—')
        page.unroute('**/api/creation?*')
        page.locator('[data-home-retry]').click()
        page.locator('#home-plans-title-panel .home-empty').wait_for()
        page.wait_for_function("() => document.querySelector('[data-home-tab=plans] .home-tab-count').textContent === '0'")
        self.assertEqual(trend_reads, [])

    def test_basket_can_open_on_different_pages_and_keeps_product_scope(self):
        page = self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.set_viewport_size({'width':390,'height':844})
        page.locator('[data-page="library"]').click()
        basket_entry=page.locator('#materials-nav')
        self.assertTrue(basket_entry.is_visible())
        self.assertTrue(basket_entry.locator('[data-icon] svg').is_visible())
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'))
        basket_entry.click()
        basket=page.locator('#material-basket-dialog')
        basket.get_by_text('That’s a lot of wobble',exact=True).wait_for()
        basket.locator('[data-material-close]').click()
        self.server.app.product_catalog.change("add", "129N")
        page.evaluate("WorkbenchShell.registerProducts(['129N'])")
        page.locator('#global-product').select_option('129N')
        basket_entry.click()
        basket.get_by_role('heading',name='还没有素材').wait_for()
        basket.locator('[data-material-close]').click()
        page.locator('#global-product').select_option('202H')
        basket_entry.click()
        basket.get_by_text('That’s a lot of wobble',exact=True).wait_for()
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'))

    def test_note_conflict_keeps_draft_for_deliberate_retry(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        basket.locator('[data-material-note]').fill('保留我的备注')
        blocked=[]
        def conflict(route):
            if route.request.method=='POST' and not blocked:
                blocked.append(True)
                route.fulfill(status=409,content_type='application/json',body=json.dumps({'error':'conflict'}))
            else:
                route.continue_()
        page.route('**/api/materials',conflict)
        basket.locator('[data-material-save]').click()
        basket.get_by_text('素材已在其他页面更新。',exact=False).wait_for()
        self.assertEqual(basket.locator('[data-material-note]').input_value(),'保留我的备注')
        basket.locator('[data-material-save]').click()
        page.locator('#toast:has-text("备注已保存")').wait_for()
        basket.locator('[data-material-close]').click()
        page.reload()
        page.locator('#rev-basket-toggle').click()
        basket.locator('[data-material-note]').wait_for()
        self.assertEqual(basket.locator('[data-material-note]').input_value(),'保留我的备注')

    def test_delayed_basket_read_cannot_replace_focused_note_before_input(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        field=basket.locator('[data-material-note]')
        field.wait_for()
        # Settle the opening read, then hold exactly the next read until focus
        # has moved to the textarea and before its first input event fires.
        page.evaluate('() => MaterialBasket.refresh()')
        held=[]
        def delay(route):
            if route.request.method=='GET':
                held.append(route)
            else:
                route.continue_()
        page.route('**/api/materials',delay)
        page.evaluate('() => { window.basketReadDone = false; MaterialBasket.refresh().then(() => { window.basketReadDone = true; }); }')
        field.focus()
        page.evaluate('window.focusedNoteNode = document.activeElement')
        self.assertEqual(len(held),1)
        held[0].continue_()
        page.wait_for_function('() => window.basketReadDone')
        self.assertTrue(page.evaluate('document.querySelector("[data-material-note]") === window.focusedNoteNode'))
        self.assertTrue(page.evaluate('document.activeElement === window.focusedNoteNode'))
        page.keyboard.insert_text('刷新不能吞掉这句备注')
        page.unroute('**/api/materials',delay)
        basket.locator('[data-material-save]').click()
        page.locator('#toast:has-text("备注已保存")').wait_for()
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'刷新不能吞掉这句备注')

    def test_note_save_keeps_text_entered_while_request_is_pending(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        field=basket.locator('[data-material-note]')
        field.wait_for()
        page.evaluate('() => MaterialBasket.refresh()')
        held=[]
        def delay(route):
            if route.request.method=='POST' and not held:
                held.append(route)
            else:
                route.continue_()
        page.route('**/api/materials',delay)
        field.fill('先保存这句')
        basket.locator('[data-material-save]').click()
        self.assertEqual(len(held),1)
        self.assertEqual(held[0].request.post_data_json['item']['note'],'先保存这句')
        field.fill('先保存这句，继续输入还没保存')
        held[0].continue_()
        page.locator('#toast:has-text("备注已保存")').wait_for()
        # The finished request saved only its own text; typing that followed is still a draft.
        self.assertEqual(field.input_value(),'先保存这句，继续输入还没保存')
        self.assertTrue(page.evaluate('MaterialBasket.hasUnsavedInput()'))
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'先保存这句')
        # Leaving the field saves the rest on top of that save, without conflicting with it.
        basket.locator('#material-basket-title').click()
        page.wait_for_function('() => !MaterialBasket.hasUnsavedInput()')
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'先保存这句，继续输入还没保存')
        self.assertTrue(basket.locator('#material-basket-error').is_hidden())

    def test_note_autosaves_on_blur_and_removal_or_clearing_can_be_undone(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        field=basket.locator('[data-material-note]')
        field.fill('离开输入框就保存')
        self.assertTrue(basket.locator('[data-material-unsaved]').is_visible())
        self.assertIn('1 条备注未保存',basket.locator('#material-selection-count').inner_text())
        basket.locator('#material-basket-title').click()
        page.wait_for_function('() => !MaterialBasket.hasUnsavedInput()')
        basket.locator('[data-material-unsaved]').wait_for(state='hidden')
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'离开输入框就保存')
        # Typing the saved text back is not an unsaved change.
        field.fill('离开输入框就保存，再改一下')
        field.fill('离开输入框就保存')
        self.assertFalse(page.evaluate('MaterialBasket.hasUnsavedInput()'))
        basket.locator('[data-material-remove]').click()
        basket.get_by_role('heading',name='还没有素材').wait_for()
        basket.get_by_role('button',name='撤销').click()
        page.locator('#toast:has-text("已恢复 1 条素材")').wait_for()
        self.assertEqual(field.input_value(),'离开输入框就保存')
        basket.locator('[data-material-clear]').click()
        basket.get_by_role('heading',name='还没有素材').wait_for()
        self.assertIn('已清空当前产品的 1 条素材',basket.locator('.material-undo').inner_text())
        page.wait_for_function("() => document.activeElement.matches('[data-material-undo]')")
        self.assertEqual(self.request('GET','/api/materials')[2]['items'],[])
        basket.get_by_role('button',name='撤销').click()
        basket.get_by_text('That’s a lot of wobble',exact=True).wait_for()
        restored=self.request('GET','/api/materials')[2]['items']
        self.assertEqual([item['note'] for item in restored],['离开输入框就保存'])

    def test_comment_theme_attaches_to_real_selling_point_and_direction(self):
        catalog=self.request('GET','/api/direction-catalog')[2]
        status,_,data=self.request('POST','/api/selling-points',{'revision':catalog['revision'],'request_id':str(uuid4()),'point':{'product':'202H','code':'S1','title':'稳定角度'}})
        self.assertEqual(status,200,data)
        point=data['point']
        catalog=self.request('GET','/api/direction-catalog')[2]
        status,_,data=self.request('POST','/api/direction-catalog',{'revision':catalog['revision'],'request_id':str(uuid4()),'direction':{'product':'202H','selling_point_id':point['id'],'concern':'夹具是否稳固','closing':'按用途选择'}})
        self.assertEqual(status,200,data)
        page=self.open_page()
        card=self.card(page,'That’s a lot of wobble')
        identifier=card.get_attribute('data-id')
        review.ReviewPageTests.card_action(self,card,'关联主题')
        dialog=page.locator('#material-theme-dialog')
        dialog.locator('select[name=target]').select_option(point['id'])
        dialog.get_by_role('button',name='保存主题关联').click()
        dialog.wait_for(state='hidden')
        review.ReviewPageTests.card_action(self,card,'关联主题')
        dialog.locator('select[name=role]').select_option('concern')
        dialog.locator('select[name=target]').select_option('202H-S1')
        dialog.get_by_role('button',name='保存主题关联').click()
        dialog.wait_for(state='hidden')
        links=self.request('GET','/api/materials')[2]['theme_links']
        selected=[link for link in links if link['review_id']==identifier]
        self.assertEqual({link['role'] for link in selected},{'selling_point','concern'})

    def test_video_hook_starts_empty_and_saves_only_explicit_quote(self):
        page=self.open_page(route='#/202H/videos',ready='.perf-tabs')
        page.locator('[data-perf-tab=list]').first.click()
        page.locator('[data-video-row] td.perf-title').first.click()  # the hook button lives in the details drawer
        page.locator('[data-video-hook]').first.click()
        dialog=page.locator('#material-hook-dialog')
        field=dialog.locator('[name=text]')
        self.assertEqual(field.input_value(),'')
        self.assertTrue(dialog.get_by_role('button',name='加入摘录篮').is_disabled())
        field.fill('原片开头用一个问题展示刀刃')
        dialog.get_by_role('button',name='加入摘录篮').click()
        dialog.wait_for(state='hidden')
        items=self.request('GET','/api/materials')[2]['items']
        hooks=[item for item in items if item['type']=='hook']
        self.assertEqual(len(hooks),1)
        self.assertEqual(hooks[0]['text'],'原片开头用一个问题展示刀刃')
        page.wait_for_function("() => document.querySelector('[data-video-hook]').textContent === '摘录钩子 · 已有 1'")

    def test_home_plan_writing_passes_explicit_plan_and_stop_has_no_ai_button(self):
        direction=creation_fixture.CreationHTTPTests.direction(self)
        saved=[]
        for action in ('execution','stop'):
            revision=self.request('GET','/api/creation')[2]['revision']
            status,_,result=self.request('POST','/api/plans',{'revision':revision,'request_id':str(uuid4()),'plan':{'product':'202H','direction_id':direction['direction_id'],'action':action,'note':'只更换原片开头','status':'pending'}})
            self.assertEqual(status,200,result)
            saved.append(result['plan'])
        page=self.open_page(route='#/202H/home',ready='#home-plans-title')
        page.locator('[data-home-tab=plans]').click()
        self.assertEqual(page.locator('[data-home-plan-write]').count(),1)
        page.evaluate('CaseAI.startFromDirection = async value => { window.chosenPlan = value; return true; }')
        page.locator('[data-home-plan-write]').click()
        page.wait_for_function('() => Boolean(window.chosenPlan)')
        chosen=page.evaluate('window.chosenPlan')
        self.assertEqual(chosen['plan_id'],saved[0]['id'])
        self.assertEqual(chosen['plan_note'],'只更换原片开头')
        self.assertEqual(chosen['product'],'202H')
        page.locator(f'[data-home-plan="{saved[0]["id"]}"][data-plan-status=done]').click()
        page.locator('[data-home-plan-write]').wait_for(state='detached')
        latest=self.request('GET','/api/creation')[2]['plans']
        self.assertEqual(next(item for item in latest if item['id']==saved[0]['id'])['status'],'done')

    def test_theme_unknown_response_retries_identical_request_without_duplicate(self):
        direction=creation_fixture.CreationHTTPTests.direction(self)
        page=self.open_page()
        review.ReviewPageTests.card_action(self,self.card(page,'That’s a lot of wobble'),'关联主题')
        dialog=page.locator('#material-theme-dialog')
        dialog.locator('select[name=target]').wait_for()
        payloads=[]
        def drop_response(route):
            payloads.append(route.request.post_data_json)
            if len(payloads)==1:
                response=route.fetch()
                self.assertEqual(response.status,200)
                route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'response lost'}))
            else:
                route.continue_()
        page.route('**/api/material-themes',drop_response)
        dialog.get_by_role('button',name='保存主题关联').click()
        dialog.get_by_role('button',name='重试同一请求').wait_for()
        self.assertTrue(dialog.locator('select[name=role]').is_disabled())
        dialog.get_by_role('button',name='重试同一请求').click()
        dialog.wait_for(state='hidden')
        self.assertEqual(len(payloads),2)
        self.assertEqual(payloads[0],payloads[1])
        self.assertEqual(len(self.request('GET','/api/materials')[2]['theme_links']),1)

    def test_all_products_view_blocks_clear_and_keeps_mixed_selection_open(self):
        page=self.open_page(route='#/all/home',ready='#home-page')
        catalog={'revision':'mixed','items':[
            {'id':'review:a','type':'review','source_id':'a','product':'202H','text':'Wobble from 202H','note':'','source_label':'tiktok_shop','unverified':True},
            {'id':'MAT-b','type':'hook','source_id':'v1','product':'129N','text':'Hook from 129N','note':''}],'theme_links':[],'limits':{'note':2000,'hook_text':6000}}
        page.route('**/api/materials',lambda route: route.fulfill(content_type='application/json',body=json.dumps(catalog)))
        page.locator('#materials-nav').click()
        basket=page.locator('#material-basket-dialog')
        basket.get_by_text('Hook from 129N',exact=True).wait_for()
        self.assertTrue(basket.locator('[data-material-clear]').is_disabled())
        self.assertTrue(basket.locator('[data-material-write]').is_disabled())
        self.assertIn('所选素材来自不同产品',basket.locator('#material-selection-count').inner_text())
        self.assertEqual(basket.locator('[data-material-note]').first.get_attribute('maxlength'),'2000')
        basket.locator('[data-material-select="MAT-b"]').uncheck()
        self.assertFalse(basket.locator('[data-material-write]').is_disabled())
        self.assertTrue(basket.locator('#material-selection-count').inner_text().startswith('已选 1 条 · 约 '))
        self.assertNotIn('不同产品',basket.locator('#material-selection-count').inner_text())
        basket.locator('[data-material-select="MAT-b"]').check()
        # Even a stale enabled button must not close the basket before the product check.
        basket.locator('[data-material-write]').evaluate('button => { button.disabled = false; button.click(); }')
        self.assertTrue(basket.is_visible())

    def test_backdrop_closes_and_undo_survives_closing_and_mobile_layout_fits_fingers(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        quote=basket.get_by_text('That’s a lot of wobble',exact=True)
        quote.wait_for()
        # Selecting text and releasing over the backdrop is not a click on the backdrop.
        box=quote.bounding_box()
        page.mouse.move(box['x']+box['width']-2,box['y']+box['height']/2)
        page.mouse.down()
        page.mouse.move(10,400)
        page.mouse.up()
        self.assertTrue(basket.is_visible())
        page.mouse.click(10,400)
        basket.wait_for(state='hidden')
        page.locator('#rev-basket-toggle').click()
        basket.locator('[data-material-remove]').click()
        basket.locator('.material-undo').wait_for()
        page.keyboard.press('Escape')
        basket.wait_for(state='hidden')
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#rev-basket-toggle').click()
        basket.get_by_role('button',name='撤销').click()
        quote.wait_for()
        gap=page.evaluate("() => { const d=document.querySelector('#material-basket-dialog'); return d.getBoundingClientRect().bottom - d.querySelector('.material-basket-footer').getBoundingClientRect().bottom; }")
        self.assertLess(gap,2)
        close=basket.locator('[data-material-close]').bounding_box()
        self.assertGreaterEqual(min(close['width'],close['height']),44)
        page.mouse.click(195,20)
        basket.wait_for(state='hidden')
        self.assertEqual(len(self.request('GET','/api/materials')[2]['items']),1)

    def test_closing_with_a_conflicting_note_says_it_is_not_saved(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        basket.locator('[data-material-note]').fill('我的备注')
        page.route('**/api/materials',lambda route: route.fulfill(status=409,content_type='application/json',body=json.dumps({'error':'conflict'})) if route.request.method=='POST' else route.continue_())
        basket.locator('#material-basket-title').click()
        basket.get_by_text('素材已在其他页面更新。',exact=False).wait_for()
        basket.locator('[data-material-close]').click()
        page.locator('#toast:has-text("1 条备注与其他页面的修改冲突，尚未保存")').wait_for()
        self.assertTrue(page.evaluate('MaterialBasket.hasUnsavedInput()'))

    def test_cancelling_the_writing_settings_returns_to_the_basket(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        basket.locator('[data-material-write]').click()
        settings=page.locator('#ai-create-dialog')
        settings.wait_for()
        basket.wait_for(state='hidden')
        page.keyboard.press('Escape')
        settings.wait_for(state='hidden')
        basket.get_by_text('That’s a lot of wobble',exact=True).wait_for()
        self.assertTrue(basket.locator('[data-material-select]').is_checked())

    def test_filters_and_select_all_act_on_shown_items_and_unticked_items_survive_reload(self):
        page=self.open_page(route='#/202H/home',ready='#home-page')
        catalog={'revision':'r','items':[
            {'id':'review:a','type':'review','source_id':'a','product':'202H','text':'Wobble on the clamp','note':'','source_label':'tiktok_shop','unverified':True,'item_revision':'1'},
            {'id':'review:b','type':'review','source_id':'b','product':'202H','text':'Edge came out mirror sharp','note':'','source_label':'amazon','unverified':True,'item_revision':'2'},
            {'id':'MAT-c','type':'hook','source_id':'v1','product':'202H','text':'Stop guessing your angle','note':'','item_revision':'3'}],
            'theme_links':[],'limits':{'note':2000,'hook_text':6000,'prompt_chars':180000}}
        page.route('**/api/materials',lambda route: route.fulfill(content_type='application/json',body=json.dumps(catalog)))
        page.locator('#materials-nav').click()
        basket=page.locator('#material-basket-dialog')
        basket.get_by_text('Stop guessing your angle',exact=True).wait_for()
        basket.locator('[data-material-filter="review"]').click()
        self.assertTrue(basket.get_by_text('Stop guessing your angle',exact=True).is_hidden())
        basket.locator('[data-material-all="0"]').click()
        self.assertTrue(basket.locator('#material-selection-count').inner_text().startswith('已选 1 条（1 条不在当前筛选中） · 约 '))
        basket.locator('[data-material-filter=""]').click()
        basket.locator('[data-material-search]').fill('mirror')
        self.assertEqual(basket.locator('.material-item:visible').count(),1)
        basket.locator('[data-material-search]').fill('nothing matches this')
        basket.locator('[data-material-nomatch]').wait_for()
        page.reload()
        page.locator('#home-page').wait_for()
        page.locator('#materials-nav').click()
        basket.get_by_text('Stop guessing your angle',exact=True).wait_for()
        self.assertEqual([basket.locator(f'[data-material-select="{identifier}"]').is_checked() for identifier in ('review:a','review:b','MAT-c')],[False,False,True])
        self.assertEqual(basket.locator('[data-material-search]').input_value(),'')

    def test_empty_basket_links_to_the_three_sources_and_closes_on_the_way(self):
        page=self.open_page(route='#/202H/home',ready='#home-page')
        page.locator('#materials-nav').click()
        basket=page.locator('#material-basket-dialog')
        basket.get_by_role('heading',name='还没有素材').wait_for()
        self.assertEqual(basket.locator('.material-empty-links a').count(),3)
        basket.get_by_role('link',name='带货数据 · 摘录钩子').click()
        basket.wait_for(state='hidden')
        page.locator('.perf-tabs').wait_for()

    def test_case_expression_button_shows_it_is_already_in_the_basket(self):
        current=copy.deepcopy(self.store.read_case(self.identifier)['current'])
        current['native_reference']={'language':'en','market':'US','duration_sec':12,'scene':'现场演示','speaker':'演示者','audience':'磨刀用户',
            'entries':[{'id':'phrase-1','channel':'speech','original':'Keep it right there.','meaning_zh':'保持在那里。','function':'动作提示','context':'当前演示','analysis':'短句','adaptation':'借用节奏','boundary':'不复制亲历','start_sec':1,'end_sec':3}]}
        self.store.append(self.identifier,{'kind':'editorial','text':'记录表达','current':current})
        # Expressions are browsed and excerpted in their dedicated library view.
        page=self.open_page(route='#/202H/library?view=native',ready='#case-search')
        page.locator('[data-native-mode=entries]').click()
        page.locator('[data-native-material]').first.wait_for()
        button=page.locator('[data-native-material]').first
        self.assertEqual(button.inner_text(),'加入摘录篮')
        button.click()
        page.locator('#toast:has-text("已加入摘录篮")').wait_for()
        page.wait_for_function("() => document.querySelector('[data-native-material]').textContent === '已在摘录篮'")
        button.click()
        page.locator('#material-basket-dialog').get_by_text('Keep it right there.',exact=True).wait_for()
        self.assertEqual(len(self.request('GET','/api/materials')[2]['items']),1)

    def test_theme_links_can_be_removed_from_the_basket_and_the_theme_dialog_with_undo(self):
        catalog=self.request('GET','/api/direction-catalog')[2]
        status,_,data=self.request('POST','/api/selling-points',{'revision':catalog['revision'],'request_id':str(uuid4()),'point':{'product':'202H','code':'S1','title':'稳定角度'}})
        self.assertEqual(status,200,data)
        page=self.open_page()
        card=self.card(page,'That’s a lot of wobble')
        card.get_by_role('button',name='摘录').click()
        card.get_by_role('button',name='已摘录').wait_for()
        theme={'revision':self.request('GET','/api/materials')[2]['revision'],'request_id':str(uuid4()),'review_id':card.get_attribute('data-id'),'product':'202H','role':'selling_point','target_id':data['point']['id']}
        self.assertEqual(self.request('POST','/api/material-themes',theme)[0],200)
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        chip=basket.get_by_role('button',name='删除主题关联：产品卖点 · S1 · 稳定角度')
        chip.click()
        basket.locator('.material-undo').get_by_text('已删除主题关联',exact=False).wait_for()
        self.assertEqual(self.request('GET','/api/materials')[2]['theme_links'],[])
        basket.get_by_role('button',name='撤销').click()
        chip.wait_for()
        self.assertEqual(len(self.request('GET','/api/materials')[2]['theme_links']),1)
        # Taking the quote out keeps its link, says so, and leaves it manageable from the theme dialog.
        basket.locator('[data-material-remove]').click()
        self.assertIn('1 条主题关联仍保留',basket.locator('.material-undo').inner_text())
        basket.locator('[data-material-close]').click()
        review.ReviewPageTests.card_action(self,card,'关联主题')
        dialog=page.locator('#material-theme-dialog')
        dialog.get_by_role('button',name='删除').click()
        dialog.get_by_text('已删除「产品卖点 · S1 · 稳定角度」',exact=True).wait_for()
        self.assertEqual(self.request('GET','/api/materials')[2]['theme_links'],[])
        dialog.get_by_role('button',name='撤销').click()
        dialog.get_by_role('button',name='删除').wait_for()
        self.assertEqual(len(self.request('GET','/api/materials')[2]['theme_links']),1)

    def pick_every_review(self):
        reviews=self.request('GET','/api/reviews')[2]['reviews']
        for item in reviews:
            self.server.app.reviews.set_pick(item['id'],True)
        return reviews

    def test_saving_or_removing_keeps_scroll_untouched_rows_and_focus(self):
        self.assertGreaterEqual(len(self.pick_every_review()),4)
        page=self.open_page()
        page.set_viewport_size({'width':1080,'height':600})
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        notes=basket.locator('[data-material-note]')
        notes.first.wait_for()
        total=notes.count()
        scroll=page.evaluate("() => { const body=document.querySelector('.material-basket-body'); body.scrollTop=body.scrollHeight; document.querySelectorAll('.material-item').forEach(node => { node.dataset.mark='1'; }); return body.scrollTop; }")
        self.assertGreater(scroll,0)
        notes.last.fill('滚动位置不变')
        basket.locator('#material-basket-title').click()
        page.wait_for_function('() => !MaterialBasket.hasUnsavedInput()')
        # Only the saved row is rebuilt; the list keeps its place.
        self.assertEqual(page.evaluate("() => document.querySelector('.material-basket-body').scrollTop"),scroll)
        self.assertEqual(page.evaluate("() => document.querySelectorAll('.material-item[data-mark]').length"),total-1)
        basket.locator('[data-material-remove]').first.focus()
        page.keyboard.press('Enter')
        basket.locator('.material-undo').wait_for()
        page.wait_for_function("() => document.activeElement.matches('[data-material-undo]')")
        self.assertEqual(basket.locator('[data-material-note]').count(),total-1)

    def test_writing_saves_all_unsaved_notes_in_one_request(self):
        self.pick_every_review()
        page=self.open_page()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        basket.locator('[data-material-note]').first.wait_for()
        # Drafts typed without leaving a field would each be autosaved on blur; set two directly instead.
        page.evaluate("() => [...document.querySelectorAll('[data-material-note]')].slice(0, 2).forEach((field, index) => { field.value = '一起保存 ' + index; field.dispatchEvent(new Event('input', {bubbles: true})); })")
        self.assertIn('2 条备注未保存',basket.locator('#material-selection-count').inner_text())
        posts=[]
        page.on('request',lambda request: posts.append(request.url.split('/api/')[-1]) if request.method=='POST' and '/api/materials' in request.url else None)
        basket.locator('[data-material-write]').click()
        page.wait_for_function('() => !MaterialBasket.hasUnsavedInput()')
        self.assertEqual(posts,['materials/notes'])
        saved=sorted(item['note'] for item in self.request('GET','/api/materials')[2]['items'] if item['note'])
        self.assertEqual(saved,['一起保存 0','一起保存 1'])

    def test_rows_have_distinct_accessible_names_and_the_count_is_announced_only_on_change(self):
        reviews=self.pick_every_review()
        page=self.open_page()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        basket.locator('[data-material-select]').first.wait_for()
        for control in ('[data-material-select]','[data-material-remove]','[data-material-save]','[data-material-theme]'):
            names=basket.locator(control).evaluate_all('nodes => nodes.map(node => node.getAttribute("aria-label"))')
            self.assertEqual(len(names),len(reviews))
            self.assertEqual(len(set(names)),len(names),control)
        first=' '.join(basket.locator('blockquote').first.inner_text().split())[:30]
        self.assertIn(first,basket.locator('[data-material-remove]').first.get_attribute('aria-label'))
        summary=basket.locator('#material-selection-count')
        self.assertEqual((summary.get_attribute('aria-live'),summary.get_attribute('aria-atomic')),('polite','true'))
        # A render that changes nothing keeps the very same text node, so nothing is read out again.
        page.evaluate("() => { window.countNode = document.querySelector('#material-selection-count').firstChild; }")
        page.evaluate('() => MaterialBasket.refresh()')
        self.assertTrue(page.evaluate("() => document.querySelector('#material-selection-count').firstChild === window.countNode"))
        basket.locator('[data-material-select]').first.uncheck()
        self.assertTrue(summary.inner_text().startswith(f'已选 {len(reviews)-1} 条'))


if __name__=='__main__':
    unittest.main()
