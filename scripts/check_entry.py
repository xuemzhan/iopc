from playwright.sync_api import sync_playwright

errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5370/", wait_until="networkidle")

    pg.fill("#input", "OPC 中枢的架构决定是什么")
    pg.keyboard.press("Enter")
    pg.wait_for_selector(".msg.ai:last-child .cites .cite", timeout=90000)
    print("答1 ok")

    pg.click(".msg.ai:last-child .cite")
    pg.wait_for_selector("#modal.on", timeout=15000)
    pg.wait_for_timeout(1000)
    print("预览:", pg.inner_text("#modal-body")[:60].replace("\n", " "))
    pg.screenshot(path=r"data\ui-preview.png")
    pg.click("#modal-close")

    pg.fill("#input", "那第二条呢？")
    pg.keyboard.press("Enter")
    pg.wait_for_selector(".msg.ai:last-child .cites .cite", timeout=120000)
    pg.wait_for_timeout(1500)
    print("答2:", pg.inner_text(".msg.ai:last-child")[:110].replace("\n", " "))
    pg.screenshot(path=r"data\ui-entry.png")
    print("errors:", errs[:3] or "none")
    b.close()
