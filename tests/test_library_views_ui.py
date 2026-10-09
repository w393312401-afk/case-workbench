"""Saved-view and library-filter checks using only disposable records."""
import copy
import json
import unittest
from urllib.parse import parse_qs

import test_server as fixture
import test_ui_regressions as browser


class LibraryViewsTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        current=copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current.update(title="Alpha 达人参考", content_origin="overseas_creator")
        current["test_direction"]={"direction_id":"202H-S1","main_difference":"定角","concern":"装夹","closing":"查看组合","variant_type":"direction"}
        self.store.append(self.identifier,{"kind":"editorial","text":"Library fixture","current":current})
        own=copy.deepcopy(current)
        own.pop("source_event_ids",None)
        own.update(title="Beta 自制参考",content_origin="self_produced")
        own["metrics"]["video_id"]="7000000000000000002"
        self.own=self.store.add({"title":own["title"],"product":"SHARPAL 202H","original":{"text":"Isolated own case","source":"test"},"feedback":{"kind":"editorial","text":"fixture"},"current":own})["case_id"]
        old=copy.deepcopy(current)
        old.pop("source_event_ids",None)
        old.update(title="Gamma 归档参考",category="高播放低转化",assessments=[])
        old["metrics"]["video_id"]="7000000000000000003"
        self.archived=self.store.add({"title":old["title"],"product":"SHARPAL 202H","original":{"text":"Isolated archived case","source":"test"},"feedback":{"kind":"editorial","text":"fixture"},"current":old})["case_id"]
        self.store.set_archived(self.archived,True,"fixture")
        other=copy.deepcopy(current)
        other.pop("source_event_ids",None)
        other.update(title="Delta 另一产品参考")
        other["test_direction"]["direction_id"]="303X-S2"
        other["metrics"]["video_id"]="7000000000000000004"
        self.other=self.store.add({"title":other["title"],"product":"SHARPAL 303X","original":{"text":"Isolated other product","source":"test"},"feedback":{"kind":"editorial","text":"fixture"},"current":other})["case_id"]
        self.legacy_id="custom-original12"
        self.legacy=json.dumps([{"id":self.legacy_id,"label":"原有达人收藏","content":"custom","icon":"folder","caseIds":[self.own]}],ensure_ascii=False)
        self.assertEqual(self.request("POST","/api/navigation/move",{"case_id":self.identifier,"revision":fixture.SERVER.revision(self.store.read_case(self.identifier)),"target":"column:"+self.legacy_id,"previous":None})[0],200)

    def open_page(self,width=1280):
        context=self.browser.new_context(viewport={"width":width,"height":900})
        self.addCleanup(context.close)
        context.add_init_script("if(localStorage.getItem('case-library-navigation-v1')===null)localStorage.setItem('case-library-navigation-v1',"+json.dumps(self.legacy)+");")
        page=context.new_page();page.set_default_timeout(10000)
        errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        self.addCleanup(lambda:self.assertEqual(errors,[],"Browser errors"))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/library")
        page.locator("#case-search").wait_for()
        return page

    def ids(self,page):
        return page.locator("[data-selection-card]").evaluate_all("nodes=>nodes.map(node=>node.dataset.selectionCard)")

    def test_factual_filters_archive_and_legacy_members_coexist_without_moving_cases(self):
        before=(self.runtime/"navigation-assignments.json").read_bytes()
        page=self.open_page()
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        self.assertEqual(page.locator("[data-move-case],[draggable=true][data-selection-card]").count(),0)
        page.locator('[data-library-preset="positive"]').click()
        page.locator("#content-origin-filter").select_option("overseas_creator")
        self.assertEqual(self.ids(page),[self.identifier])
        page.locator("#direction-filter").select_option("202H-S1")
        self.assertEqual(self.ids(page),[self.identifier])
        page.locator("#archive-category-filter").select_option("mixed")
        self.assertEqual(self.ids(page),[])
        page.locator("#archive-status-filter").select_option("all")
        self.assertEqual(self.ids(page),[self.archived])
        page.locator(f'[data-case-select="{self.archived}"]').check()
        self.assertFalse(page.locator("#bulk-restore").is_disabled())
        self.assertFalse(page.locator("#bulk-delete").is_disabled())
        page.locator("#library-saved-view").select_option(self.legacy_id)
        page.wait_for_function("id=>state.view===id",arg=self.legacy_id)
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        self.assertEqual((self.runtime/"navigation-assignments.json").read_bytes(),before)
        self.assertFalse(self.store.read_case(self.identifier)["archived"])

    def test_save_restore_rename_delete_filters_and_leave_records_unchanged(self):
        before={case:self.store.read_case(case) for case in [self.identifier,self.own,self.archived,self.other]}
        assignment=(self.runtime/"navigation-assignments.json").read_bytes()
        page=self.open_page(width=390)
        page.locator("#content-origin-filter").select_option("overseas_creator")
        page.locator("#archive-category-filter").select_option("positive")
        page.locator("#direction-filter").select_option("202H-S1")
        page.locator("#library-save-view-button").click()
        page.locator('#library-save-view [name="view_name"]').fill("达人正向筛选")
        page.get_by_role("button",name="保存视图",exact=True).click()
        page.wait_for_function("()=>document.querySelector('#library-saved-view').value.startsWith('custom-') && document.querySelector('#library-saved-view').value!=='custom-original12'")
        saved_id=page.locator("#library-saved-view").input_value()
        page.reload();page.locator("#case-search").wait_for()
        self.assertEqual(page.locator("#content-origin-filter").input_value(),"overseas_creator")
        self.assertEqual(page.locator("#archive-category-filter").input_value(),"positive")
        self.assertEqual(page.locator("#direction-filter").input_value(),"202H-S1")
        self.assertEqual(self.ids(page),[self.identifier])
        page.locator("#global-product").select_option("")
        page.wait_for_function("()=>document.querySelector('#global-product').value==='' && document.querySelector('#library-saved-view').value===''")
        self.assertEqual(page.locator("#direction-filter").input_value(),"")
        self.assertEqual(set(self.ids(page)),{self.identifier,self.other})
        page.reload();page.locator("#case-search").wait_for()
        self.assertEqual(page.locator("#global-product").input_value(),"")
        self.assertEqual(page.locator("#direction-filter").input_value(),"")
        self.assertEqual(set(self.ids(page)),{self.identifier,self.other})
        page.locator("#library-saved-view").select_option(saved_id)
        page.wait_for_function("()=>document.querySelector('#global-product').value==='202H'")
        self.assertEqual(page.locator("#direction-filter").input_value(),"202H-S1")
        self.assertEqual(self.ids(page),[self.identifier])
        page.locator("#case-search").fill("no result")
        page.locator("#result-count").get_by_text("0 条案例",exact=True).wait_for()
        self.assertEqual(page.locator("#library-saved-view").input_value(),"")
        page.locator("#library-saved-view").select_option(saved_id)
        page.wait_for_function("()=>document.querySelector('#case-search').value===''")
        self.assertEqual(self.ids(page),[self.identifier])
        page.locator("#library-manage-views").click()
        row=page.locator(f'#library-view-manager [data-view-id="{saved_id}"]')
        row.locator("input").fill("改名后的视图")
        row.locator("[data-rename-view]").click()
        self.assertEqual(row.locator("input").input_value(),"改名后的视图")
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
        row.locator("[data-delete-view]").click()
        self.assertTrue(row.locator("input").is_disabled())
        row.locator("[data-restore-view]").click()
        self.assertFalse(row.locator("input").is_disabled())
        page.get_by_role("button",name="完成",exact=True).click()
        stored=json.loads(page.evaluate("localStorage.getItem('case-library-navigation-v1')"))
        legacy=next(item for item in stored if item["id"]==self.legacy_id)
        self.assertEqual((legacy["label"],legacy["caseIds"]),("原有达人收藏",[self.own]))
        self.assertEqual((self.runtime/"navigation-assignments.json").read_bytes(),assignment)
        self.assertEqual({case:self.store.read_case(case) for case in before},before)

    def test_universal_scope_is_independent_of_renamed_or_deleted_legacy_all(self):
        old_all={"id":"all","label":"旧的精选收纳","content":"custom","caseIds":[self.own]}
        self.legacy=json.dumps([old_all,*json.loads(self.legacy)],ensure_ascii=False)
        assignment=(self.runtime/"navigation-assignments.json").read_bytes()
        page=self.open_page()
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        page.locator("#library-saved-view").select_option("all")
        page.wait_for_function("()=>document.querySelector('#library-saved-view').value==='all'")
        self.assertEqual(self.ids(page),[self.own])
        page.reload();page.locator("#case-search").wait_for()
        self.assertEqual(self.ids(page),[self.own])
        page.locator('[data-library-preset="all"]').click()
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        page.locator("#library-saved-view").select_option("all")
        page.wait_for_function("()=>document.querySelector('#library-saved-view').value==='all'")
        page.locator("#library-clear-filters").click()
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        page.locator("#library-saved-view").select_option("all")
        page.wait_for_function("()=>document.querySelector('#library-saved-view').value==='all'")
        page.locator("#library-manage-views").click()
        page.locator('#library-view-manager [data-view-id="all"] [data-delete-view]').click()
        page.get_by_role("button",name="完成",exact=True).click()
        page.reload();page.locator("#case-search").wait_for()
        self.assertEqual(page.evaluate("state.view"),"reference-all")
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        page.locator('[data-library-preset="positive"]').click()
        page.locator("#library-clear-filters").click()
        self.assertEqual(set(self.ids(page)),{self.identifier,self.own})
        stored=json.loads(page.evaluate("localStorage.getItem('case-library-navigation-v1')"))
        kept=next(item for item in stored if item["id"]=="all")
        self.assertTrue(kept["deleted"])
        self.assertEqual((kept["content"],kept["caseIds"]),("custom",[self.own]))
        self.assertEqual((self.runtime/"navigation-assignments.json").read_bytes(),assignment)


if __name__=="__main__":unittest.main()
