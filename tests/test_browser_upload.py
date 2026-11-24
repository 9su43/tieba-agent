"""Synthetic local HTML only: no Tieba login, account or real publishing."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import browser_service
import settings

@unittest.skipUnless(os.environ.get('TIEBA_BROWSER_TEST')=='1','Set TIEBA_BROWSER_TEST=1 after installing Chromium')
class BrowserUpload(unittest.TestCase):
    def test_file_upload_and_text_entry_without_system_clipboard(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);images=root/'state'/'repost-images';images.mkdir(parents=True)
            fixture=images/'fixture.png';Image.new('RGB',(4,4),(0,100,255)).save(fixture)
            cfg=settings.merge(settings.DEFAULTS,{'browser':{'headless':True,'channel':os.environ.get('TIEBA_TEST_BROWSER_CHANNEL','')}})
            with patch.object(browser_service,'ROOT',root),patch.object(settings,'load',lambda:cfg):
                b=browser_service.Browser()
                try:
                    b.ensure();b.page.set_content('<input type="file" accept="image/*"><div id="editor" contenteditable="true"></div>')
                    b.command('set_clipboard_image',{'path':str(fixture)});b.command('send_keys',{'keys':'Mod+V'})
                    self.assertEqual(b.page.locator('input').evaluate('(el)=>el.files[0].name'),'fixture.png')
                    b.page.locator('#editor').focus();b.command('set_clipboard_text',{'text':'隔离文本'});b.command('send_keys',{'keys':'Mod+V'})
                    self.assertEqual(b.page.locator('#editor').inner_text(),'隔离文本')
                finally:b.close()
