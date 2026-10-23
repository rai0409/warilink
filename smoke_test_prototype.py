from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parent
html=(root/'prototype/index.html').read_text(encoding='utf-8')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
    desktop=browser.new_page(viewport={'width':1440,'height':980},device_scale_factor=1)
    errors=[]
    desktop.on('pageerror',lambda e: errors.append(str(e)))
    desktop.set_content(html,wait_until="load")
    desktop.screenshot(path=str(root/'prototype/desktop_preview.png'),full_page=True)
    assert desktop.get_by_text('みんなの支出を、ひとつに。').is_visible()
    desktop.locator('#desktopnav').get_by_role('button',name='OCR').click()
    desktop.locator('#mockOCR').click()
    assert desktop.locator('#ocrResults').is_visible()
    desktop.locator('#confirmOCR').click()
    desktop.locator('#recalcShares').click()
    assert desktop.get_by_text('全員の負担額合計が2,970円と一致しました（デモ）。').is_visible()
    desktop.locator('#desktopnav').get_by_role('button',name='精算').click()
    desktop.locator('#reportPaid').click()
    assert desktop.get_by_text('支払者が送金を申告しました').is_visible()
    desktop.locator('#simulateMatch').click()
    assert desktop.locator('#payStatus').inner_text()=='銀行入金照合済み'
    desktop.locator('#desktopnav').get_by_role('button',name='受取設定').click()
    desktop.locator('#toggleAccount').click()
    assert desktop.locator('#bankAccount').inner_text()=='•••• 1234'
    assert not errors, errors
    mobile=browser.new_page(viewport={'width':390,'height':844},device_scale_factor=1)
    mobile_errors=[]
    mobile.on('pageerror',lambda e:mobile_errors.append(str(e)))
    mobile.set_content(html,wait_until="load")
    mobile.screenshot(path=str(root/'prototype/mobile_preview.png'),full_page=True)
    assert mobile.locator('.mobile-nav').is_visible()
    assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1'), 'horizontal overflow'
    mobile.locator('#mobilenav').get_by_role('button',name='精算').click()
    mobile.screenshot(path=str(root/'prototype/mobile_settlement_preview.png'),full_page=True)
    assert mobile.get_by_text('支払いを、見える化する。').is_visible()
    assert not mobile_errors,mobile_errors
    print('PASS: responsive UI, no JS errors, receipt mock, allocation sum, bank evidence demo, account toggle')
    browser.close()
