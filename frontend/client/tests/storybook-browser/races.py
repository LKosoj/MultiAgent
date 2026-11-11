"""Browser regression for resource selection; run with the repository .venv Python."""
import functools
import http.server
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

CLIENT = Path(__file__).resolve().parents[2]
ENTRY = Path(__file__).with_name("races-entry.tsx")
with tempfile.TemporaryDirectory(prefix="storybook-races-") as directory:
    root = Path(directory)
    subprocess.run([str(CLIENT / "node_modules/esbuild/bin/esbuild"), str(ENTRY), "--bundle", "--platform=browser", "--outfile=" + str(root / "bundle.js")], check=True, env={**os.environ, "NODE_PATH": str(CLIENT / "node_modules")})
    (root / "index.html").write_text('<div id="root"></div><script src="/bundle.js"></script>')
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path="/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome")
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            resources = page.locator("#resources")
            series = page.locator("#series")
            def resolve(action, ident, value):
                page.evaluate("([a,i,v]) => window.resolveRequest(a,i,v)", [action, ident, value])
            resources.get_by_role("button", name="A", exact=True).click()
            save = resources.get_by_role("button", name="Сохранить новую версию")
            expect(save).to_be_disabled()
            expect(resources.get_by_label("Название", exact=True).last).to_be_disabled()
            save.dispatch_event("click")
            assert page.evaluate("() => window.requestCalls().filter(x => x.action === 'shared_resource_save').length") == 0
            resolve("shared_resource_get", "A", {"data": {"name": "Альфа", "voice": "голос"}, "reference_path": "a.png"})
            expect(save).to_be_disabled()
            resolve("token", "token", None)
            expect(save).to_be_enabled()
            save.click()
            saved = page.evaluate("() => window.requestCalls().filter(x => x.action === 'shared_resource_save').at(-1).payload")
            assert saved["entity_id"] == "A" and saved["data"] == {"name": "Альфа", "voice": "голос"}
            resources.get_by_role("button", name="A", exact=True).click()
            page.evaluate("() => window.rejectRequest('shared_resource_get', 'A')")
            expect(resources.get_by_role("alert")).to_contain_text("Ошибка загрузки")
            expect(save).to_be_disabled()
            resources.get_by_role("button", name="A", exact=True).click()
            resources.get_by_role("button", name="B", exact=True).click()
            resolve("shared_resource_get", "B", {"data": {"name": "Бета"}, "reference_path": None})
            resolve("shared_resource_get", "A", {"data": {"name": "Альфа"}, "reference_path": None})
            expect(resources.get_by_label("Название", exact=True).last).to_have_value("Бета")
            resources.get_by_role("button", name="A", exact=True).click()
            resolve("shared_resource_get", "A", {"data": {"name": "Альфа"}, "reference_path": "a.png"})
            resources.get_by_role("button", name="B", exact=True).click()
            resolve("shared_resource_get", "B", {"data": {"name": "Бета"}, "reference_path": None})
            resolve("token", "token", None)
            expect(resources.get_by_label("Название", exact=True).last).to_have_value("Бета")
            assert resources.locator('img[alt="Образец ресурса"]').count() == 0
            resources.get_by_role("button", name="A", exact=True).click()
            resolve("shared_resource_get", "A", {"data": {"name": "Альфа"}, "reference_path": "a.png"})
            resolve("token", "token", None)
            expect(resources.locator('img[alt="Образец ресурса"]')).to_have_count(1)
            sample = root / "sample.png"
            sample.write_bytes(b"fake png")
            resources.locator('input[type="file"]').set_input_files(str(sample))
            resources.get_by_label("Тип").select_option("locations")
            assert resources.locator('input[type="file"]').input_value() == ""
            assert resources.locator('img[alt="Образец ресурса"]').count() == 0
            assert resources.get_by_label("Название", exact=True).last.input_value() == ""
            resources.get_by_label("Идентификатор").fill("place")
            resources.locator('input[type="file"]').set_input_files(str(sample))
            resources.get_by_label("Название", exact=True).last.fill("Место")
            resources.get_by_label("Идентификатор").fill("other")
            assert resources.locator('input[type="file"]').input_value() == ""
            assert resources.get_by_label("Название", exact=True).last.input_value() == ""
            resources.get_by_role("button", name="Сохранить новую версию").click()
            saved = page.evaluate("() => window.requestCalls().filter(x => x.action === 'shared_resource_save').at(-1).payload")
            assert saved["entity_id"] == "other" and saved["data"] == {} and saved["reference_base64"] == ""
            series.get_by_text("characters / A:").get_by_role("button", name="Выбрать версию").click()
            resolve("shared_resource_versions", "A", {"versions": ["v2"]})
            series.get_by_role("button", name="v2").click()
            series.get_by_text("characters / B:").get_by_role("button", name="Выбрать версию").click()
            resolve("series_resource_update_preview", "A", {"affected_results": ["A"]})
            resolve("shared_resource_versions", "B", {"versions": ["v3"]})
            assert series.get_by_role("button", name="Применить версию").count() == 0
            series.get_by_role("button", name="v3").click()
            resolve("series_resource_update_preview", "B", {"affected_results": ["B"]})
            series.get_by_text("Затронутые результаты: B").wait_for(state="visible")
            series.get_by_text("characters / A:").get_by_role("button", name="Выбрать версию").click()
            assert series.get_by_role("button", name="Применить версию").count() == 0
            browser.close()
    finally:
        server.shutdown()
