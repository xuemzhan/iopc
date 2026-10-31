from playwright.sync_api import sync_playwright

errs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5370/", wait_until="networkidle")

    # 1) 问一句，产生会话
    pg.fill("#input", "OPC 中枢的架构决定是什么")
    pg.keyboard.press("Enter")
    pg.wait_for_selector(".msg.ai:last-child .cites .cite", timeout=90000)
    print("ask ok")

    # 2) 对话 tab 应有一条
    pg.click('.tab[data-tab="conv"]')
    pg.wait_for_timeout(800)
    n = pg.locator("#pane-conv .conv").count()
    print("conversations in list:", n)
    first = pg.inner_text("#pane-conv .conv:first-child .t")
    print("first conv title:", first)

    # 3) 刷新后还能载入
    pg.reload(wait_until="networkidle")
    pg.click('.tab[data-tab="conv"]')
    pg.wait_for_timeout(800)
    pg.click("#pane-conv .conv:first-child")
    pg.wait_for_timeout(1200)
    msgs = pg.locator("#log .msg").count()
    print("messages after reload:", msgs)
    pg.screenshot(path=r"data\ui-history.png")

    # 4) 异步捕获：应出现进度条
    pg.fill("#input", "记一条：观察者模式适合做事件通知，但要注意内存泄漏。")
    pg.keyboard.press("Enter")
    pg.wait_for_selector(".msg.ai:last-child .bar", timeout=30000)
    print("progress bar shown:", pg.inner_text(".msg.ai:last-child .stage"))
    pg.wait_for_selector(".msg.ai:last-child .card", timeout=180000)
    print("capture card:", pg.inner_text(".msg.ai:last-child .card h4"))
    pg.screenshot(path=r"data\ui-async.png")
    print("errors:", errs[:3] or "none")
    b.close()
