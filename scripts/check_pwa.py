from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:5370/", wait_until="networkidle")
    pg.wait_for_timeout(1800)
    print("manifest:", pg.get_attribute("link[rel=manifest]", "href"))
    print("theme-color:", pg.get_attribute("meta[name=theme-color]", "content"))
    print("sw:", pg.evaluate("async () => { const r = await navigator.serviceWorker.getRegistration(); return r ? (r.active ? 'active' : 'installing') : 'none'; }"))
    print("caches:", pg.evaluate("async () => (await caches.keys())"))
    b.close()
