        for _intento in range(3):
            try:
                await page.goto(INDEX_URL, wait_until="domcontentloaded", timeout=60000)
                _goto_ok = True
                break
            except Exception:
                if _intento == 2: print("La Rioja no responde, sigo con la siguiente"); return
                await page.wait_for_timeout(5000)
