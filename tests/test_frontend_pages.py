#!/usr/bin/env python3
"""Smoke test: confirm cluster-hub /frontend/* pages actually render."""
import asyncio
from playwright.async_api import async_playwright

PAGES = [
    ("authors",     "http://localhost:8090/frontend/authors.html"),
    ("diff",        "http://localhost:8090/frontend/diff.html"),
    ("journal",     "http://localhost:8090/frontend/journal.html"),
    ("node-view",   "http://localhost:8090/frontend/node-view.html"),
    ("bulk-export", "http://localhost:8090/frontend/bulk-action-export.html"),
    ("conversations","http://localhost:8090/frontend/conversations.html"),
]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        passed = 0
        for name, url in PAGES:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            try:
                resp = await page.goto(url, timeout=10000, wait_until="domcontentloaded")
                title = await page.title()
                await page.wait_for_timeout(1500)
                body_len = await page.evaluate("() => document.body ? document.body.innerText.length : 0")
                has_h1 = await page.evaluate("() => !!document.querySelector('h1')")
                ok = resp.status == 200 and body_len > 100
                # Pages intentionally fall back to mock data when the
                # cluster-hub doesn't proxy /api/* → 404 is expected,
                # not an error.
                real_errors = [e for e in errors if '404' not in e]
                if ok and not real_errors: passed += 1
                print(f"  {'✅' if ok and not real_errors else '❌'} {name:14} status={resp.status} title='{title[:40]}' body={body_len}c h1={has_h1} errs={len(real_errors)}")
                if real_errors: print(f"      {real_errors[0][:150]}")
            except Exception as e:
                print(f"  ❌ {name:14} ERROR: {str(e)[:150]}")
            await page.close()
        await browser.close()
        print(f"\n  RESULT: {passed}/{len(PAGES)} pages render cleanly")

asyncio.run(main())
