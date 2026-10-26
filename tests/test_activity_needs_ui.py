"""The selected task's current needs update when its saved status changes."""
from copy import deepcopy
import unittest
from playwright.sync_api import expect
import test_task_activity_ui as fixture


class ActivityNeedsBrowserTests(unittest.TestCase):
    setUpClass=classmethod(fixture.TaskActivityBrowserTests.setUpClass.__func__)
    tearDownClass=classmethod(fixture.TaskActivityBrowserTests.tearDownClass.__func__)
    setUp=fixture.TaskActivityBrowserTests.setUp
    add_run=fixture.TaskActivityBrowserTests.add_run
    counts=fixture.TaskActivityBrowserTests.counts
    respond=fixture.TaskActivityBrowserTests.respond
    page=fixture.TaskActivityBrowserTests.page
    refresh=fixture.TaskActivityBrowserTests.refresh

    def production_run(self,status):
        run=self.add_run('edit:one','202H',status)
        run.update(kind='edit',owner_id='PRD-0123456789ab',source_id='EDT-0123456789ab',url='#/202H/production/PRD-0123456789ab?step=edit')
        self.changes[-1]['run']=deepcopy(run)
        return run

    def test_running_task_refreshes_specific_saved_gaps_without_reselection(self):
        self.production_run('running')
        record={'id':'PRD-0123456789ab','product':'202H','selected_take':'TAK-one',
                'takes':[{'id':'TAK-one','groups':[]}],
                'timelines':[{'unique_id':'timeline','take_id':'TAK-one','fps':'30'}],
                'edits':[{'id':'EDT-0123456789ab','timeline_uid':'timeline','status':'running'}]}
        reads=[]
        def production(route):
            reads.append(route.request.method)
            route.fulfill(json={'job':deepcopy(record)})
        self.context.route('**/api/production/jobs/**',production)
        page=self.page()
        page.add_script_tag(content="window.esc=value=>String(value??'').replace(/</g,'&lt;'); WorkbenchShell.matchesProduct=(a,b)=>a===b; WorkbenchShell.url=(hash,product)=>'#/'+product+'/'+hash.slice(1);")
        page.add_style_tag(path=str(fixture.ROOT/'static/production-needs.css'))
        page.add_script_tag(path=str(fixture.ROOT/'static/production-needs.js'))
        page.locator('#task-activity-open').click()
        page.wait_for_function("document.querySelector('.activity-needs')?.dataset.needsSequence==='1'")
        expect(page.locator('.activity-needs')).to_be_hidden()
        record['edits'][0].update(status='needs_material',report={'gaps':[{'start':30,'end':60,'need':'已完成运行后保存的缺素材区间'}]})
        self.production_run('needs_attention')
        self.refresh(page)
        expect(page.locator('.activity-needs [data-need-key="gap-EDT-0123456789ab-0"]')).to_contain_text('已完成运行后保存的缺素材区间')
        self.assertGreaterEqual(len(reads),2)
        self.assertTrue(all(method=='GET' for method in reads))


if __name__=='__main__':
    unittest.main()
