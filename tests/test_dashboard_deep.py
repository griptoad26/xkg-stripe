#!/usr/bin/env python3
"""Deep dashboard test — does the React app actually mount and render?"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        errors = []
        net_errors = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("requestfailed", lambda req: net_errors.append(f"{req.method} {req.url} → {req.failure}"))
        
        print("[1] Loading /dashboard/")
        resp = await page.goto("http://localhost:8090/dashboard/", timeout=15000, wait_until="networkidle")
        print(f"  status: {resp.status}, url: {page.url}")
        
        # Give React 3s to mount
        await page.wait_for_timeout(3000)
        
        # Check if React rendered anything into #root or <body>
        body_len = await page.evaluate("() => document.body ? document.body.innerText.length : 0")
        body_html_len = await page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
        has_root = await page.evaluate("() => !!document.getElementById('root')")
        root_children = await page.evaluate("() => { const r = document.getElementById('root'); return r ? r.children.length : 0; }")
        h1 = await page.evaluate("() => { const h = document.querySelector('h1'); return h ? h.innerText : null; }")
        title = await page.title()
        visible_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 200) : ''")
        
        print(f"[2] After load")
        print(f"  title: '{title}'")
        print(f"  body innerText: {body_len} chars")
        print(f"  body innerHTML: {body_html_len} chars")
        print(f"  has #root: {has_root}, root.children: {root_children}")
        print(f"  h1: {h1!r}")
        print(f"  visible text: {visible_text!r}")
        print(f"[3] Errors: {len(errors)}")
        for e in errors[:5]: print(f"  - {e[:200]}")
        print(f"[4] Network failures: {len(net_errors)}")
        for e in net_errors[:5]: print(f"  - {e[:200]}")
        
        # Take a screenshot for visual review
        await page.screenshot(path="/tmp/dashboard.png", full_page=True)
        print(f"[5] Screenshot saved: /tmp/dashboard.png")
        
        await browser.close()
        print(f"\n{'✅' if (body_len > 100 and root_children > 0 and not errors) else '❌'} Dashboard renders: {body_len > 100 and root_children > 0 and not errors}")

asyncio.run(main())
