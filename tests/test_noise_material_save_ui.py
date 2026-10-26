"""Focused note-save safety regression using only the disposable review fixture."""
import unittest
import test_review_ui as fixture
from test_noise_library_voice_ui import capture_fixture

class NoiseMaterialSaveTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ReviewPageTests.tearDownClass.__func__)
    setUp = fixture.ReviewPageTests.setUp
    stop_server = fixture.ReviewPageTests.stop_server
    request = fixture.ReviewPageTests.request
    open_page = fixture.ReviewPageTests.open_page
    card = fixture.ReviewPageTests.card

    def test_explicit_retry_does_not_race_the_note_autosave(self):
        page=self.open_page()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        field=page.locator('[data-material-note]')
        field.wait_for()
        page.evaluate('''() => {
          window.noteTrace=[];
          for (const name of ['pointerdown','focusout','focusin','click']) document.addEventListener(name,e=>{
            if(e.target.closest('#material-basket-dialog')) window.noteTrace.push([name,e.target.tagName,e.target.getAttribute('data-material-save'),e.relatedTarget?.tagName,e.relatedTarget?.getAttribute?.('data-material-save')]);
          },true);
          window.noteToasts=[];const old=toast;toast=(...args)=>{window.noteToasts.push(args);return old(...args);};
        }''')
        field.fill('fixture note')
        page.locator('[data-material-save]').click()
        try:
            page.wait_for_function('() => !MaterialBasket.hasUnsavedInput()', timeout=10000)
        except Exception:
            self.fail(str({"trace":page.evaluate('window.noteTrace'), "toasts":page.evaluate('window.noteToasts'), "error":page.locator('#material-basket-error').inner_text(), "saved":self.request('GET','/api/materials')[2]['items'][0]['note']}))
        self.assertEqual(self.request('GET','/api/materials')[2]['items'][0]['note'],'fixture note')
        self.assertIn(['备注已保存'],page.evaluate('window.noteToasts'),page.evaluate('window.noteTrace'))

    def test_theme_editor_replaces_mobile_basket_and_returns_on_close(self):
        page=self.open_page(width=390)
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='摘录').click()
        self.card(page,'That’s a lot of wobble').get_by_role('button',name='已摘录').wait_for()
        page.locator('#rev-basket-toggle').click()
        basket=page.locator('#material-basket-dialog')
        basket.locator('[data-material-theme]').click()
        page.locator('#material-theme-dialog[open]').wait_for()
        self.assertFalse(basket.evaluate('node=>node.open'))
        self.assertEqual(page.locator('dialog:modal').count(),1)
        capture_fixture(page,"basket-theme-mobile-fixture.png")
        page.locator('#material-theme-dialog [data-theme-close]').click()
        basket.locator('[data-material-note]').wait_for()
        self.assertTrue(basket.evaluate('node=>node.open'))
        self.assertEqual(page.locator('dialog:modal').count(),1)

if __name__=='__main__': unittest.main()
