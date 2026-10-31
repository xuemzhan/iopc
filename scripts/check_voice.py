import time

from playwright.sync_api import sync_playwright

WAV = r"data\tts-test.wav"
errs = []
with sync_playwright() as p:
    b = p.chromium.launch(args=[
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={WAV}",
    ])
    ctx = b.new_context(viewport={"width": 1500, "height": 950},
                        permissions=["microphone"])
    pg = ctx.new_page()
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5370/", wait_until="networkidle")

    pg.click("#mic")
    time.sleep(5)
    pg.click("#mic")

    pg.wait_for_selector(".msg.ai:last-child .bar, .msg.ai:last-child .card", timeout=60000)
    print("upload started:", pg.inner_text(".msg.ai:last-child")[:80].replace("\n", " "))
    try:
        pg.wait_for_selector(".msg.ai:last-child .card", timeout=180000)
        print("card:", pg.inner_text(".msg.ai:last-child")[:180].replace("\n", " "))
    except Exception as e:
        print("no card:", pg.inner_text(".msg.ai:last-child")[:200].replace("\n", " "))
    pg.screenshot(path=r"data\ui-voice.png")
    print("errors:", errs[:3] or "none")
    b.close()
