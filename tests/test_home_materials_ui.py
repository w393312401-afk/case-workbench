"""Phase four home/basket checks with isolated libraries and browser profiles."""
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

    def test_home_has_four_product_charts_and_mobile_has_no_page_overflow(self):
        page = self.open_page(route='#/202H/home', ready='#home-weekly-title')
        self.assertEqual(page.locator('.home-weekly-charts .perf-mini').count(), 4)
        self.assertIn('2026-09-13', page.locator('#home-page').inner_text())
        self.assertIn('完整系统价值', page.locator('.home-case-list').inner_text())
        self.assertIn('后台数据截至', page.locator('#home-page').inner_text())
        page.set_viewport_size({'width':390,'height':844})
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'))
        page.evaluate("WorkbenchShell.registerProducts(['129N'])")
        page.locator('#global-product').select_option('129N')
        page.get_by_text('当前产品没有可用周数据。', exact=True).wait_for()
        self.assertEqual(page.locator('.home-weekly-charts').count(), 0)

    def test_basket_can_open_on_different_pages_and_keeps_product_scope(self):
        page = self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.set_viewport_size({'width':390,'height':844})
        page.locator('[data-page="library"]').click()
        basket_entry=page.locator('.sidebar>#materials-nav')
        self.assertTrue(basket_entry.is_visible())
        self.assertTrue(basket_entry.locator('[data-icon] svg').is_visible())
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'))
        basket_entry.click()
        basket=page.locator('#material-basket-dialog')
        basket.get_by_text('That’s a lot of wobble',exact=True).wait_for()
        basket.locator('[data-material-close]').click()
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
            if route.request.method=='POST':
                held.append(route)
            else:
                route.continue_()
        page.route('**/api/materials',delay)
        field.fill('先保存这句')
        basket.locator('[data-material-save]').click()
        self.assertEqual(len(held),1)
        field.fill('先保存这句，继续输入还没保存')
        # Move focus away so the completed request also exercises rerendering.
        basket.locator('#material-basket-title').click()
        held[0].continue_()
        page.locator('#toast:has-text("备注已保存")').wait_for()
        self.assertEqual(field.input_value(),'先保存这句，继续输入还没保存')
        self.assertTrue(page.evaluate('MaterialBasket.hasUnsavedInput()'))
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'先保存这句')
        basket.locator('[data-material-close]').click()
        page.locator('#rev-basket-toggle').click()
        self.assertEqual(field.input_value(),'先保存这句，继续输入还没保存')
        page.unroute('**/api/materials',delay)
        basket.locator('[data-material-save]').click()
        page.wait_for_function('() => !MaterialBasket.hasUnsavedInput()')
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'先保存这句，继续输入还没保存')

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
        card.get_by_role('button',name='关联主题').click()
        dialog=page.locator('#material-theme-dialog')
        dialog.locator('select[name=target]').select_option(point['id'])
        dialog.get_by_role('button',name='保存主题关联').click()
        dialog.wait_for(state='hidden')
        card.get_by_role('button',name='关联主题').click()
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
        page.locator('[data-video-hook]').first.click()
        dialog=page.locator('#material-hook-dialog')
        field=dialog.locator('[name=text]')
        self.assertEqual(field.input_value(),'')
        self.assertTrue(dialog.get_by_role('button',name='加入素材篮').is_disabled())
        field.fill('原片开头用一个问题展示刀刃')
        dialog.get_by_role('button',name='加入素材篮').click()
        dialog.wait_for(state='hidden')
        items=self.request('GET','/api/materials')[2]['items']
        hooks=[item for item in items if item['type']=='hook']
        self.assertEqual(len(hooks),1)
        self.assertEqual(hooks[0]['text'],'原片开头用一个问题展示刀刃')

    def test_home_plan_writing_passes_explicit_plan_and_stop_has_no_ai_button(self):
        direction=creation_fixture.CreationHTTPTests.direction(self)
        saved=[]
        for action in ('execution','stop'):
            revision=self.request('GET','/api/creation')[2]['revision']
            status,_,result=self.request('POST','/api/plans',{'revision':revision,'request_id':str(uuid4()),'plan':{'product':'202H','direction_id':direction['direction_id'],'action':action,'note':'只更换原片开头','status':'pending'}})
            self.assertEqual(status,200,result)
            saved.append(result['plan'])
        page=self.open_page(route='#/202H/home',ready='#home-plans-title')
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
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='关联主题').click()
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


if __name__=='__main__':
    unittest.main()
