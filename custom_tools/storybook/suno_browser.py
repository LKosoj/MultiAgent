"""Suno generation and official MP3 downloads through its web interface."""

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


@contextmanager
def _browser_page(cookies):
    from cloakbrowser import launch

    display = None
    browser = None
    env = os.environ.copy()
    try:
        if not env.get("DISPLAY"):
            display = subprocess.Popen(
                ["Xvfb", "-displayfd", "1", "-screen", "0", "1280x900x24"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            number = display.stdout.readline().strip()
            if not number:
                raise RuntimeError("Cannot start Xvfb for the Suno browser")
            env["DISPLAY"] = f":{number}"
        browser = launch(headless=False, humanize=True, browser_version="146.0.7680.177.5", env=env)
        context = browser.new_context(accept_downloads=True)
        context.add_cookies([
            {"name": name, "value": value, "domain": ".suno.com", "path": "/",
             "secure": True, "sameSite": "None"}
            for name, value in cookies.items()
        ])
        yield context.new_page()
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            if display is not None:
                display.terminate()
                display.wait(timeout=10)


def _open(page, url):
    page.goto(url, wait_until="load", timeout=60000)
    page.wait_for_timeout(4000)
    reject = page.get_by_role("button", name="Deny all optional cookies", exact=True)
    if reject.count() and reject.is_visible():
        reject.click()


def _generation_request(url):
    parsed = urlsplit(url)
    return (parsed.hostname, parsed.path) in {
        ("studio-api.prod.suno.com", "/api/generate/v2/"),
        ("studio-api.prod.suno.com", "/api/generate/v2-web/"),
        ("suno.com", "/a/api/generate/v2-web/"),
    }


def _click_verification(page):
    for frame in page.frames:
        if urlsplit(frame.url).hostname != "challenges.cloudflare.com":
            continue
        checkbox = frame.get_by_role("checkbox")
        if checkbox.count() and checkbox.first.is_visible():
            checkbox.first.click(timeout=5000)
            return True
        box = frame.frame_element().bounding_box()
        if box and box["width"] >= 250 and 40 <= box["height"] <= 100:
            page.mouse.click(box["x"] + 27, box["y"] + box["height"] / 2)
            return True
    return False


def submit_music(cookies, payload):
    with _browser_page(cookies) as page:
        _open(page, "https://suno.com/create")
        result = {}

        def prepare(route):
            request = route.request
            if request.method != "POST" or not _generation_request(request.url):
                route.continue_()
                return
            body = request.post_data_json
            body.update({key: value for key, value in payload.items() if key != "token"})
            if "tags" in payload:
                body.setdefault("metadata", {})["create_mode"] = "custom"
                body.pop("gpt_description_prompt", None)
            route.continue_(post_data=json.dumps(body))

        def receive(response):
            if response.request.method != "POST" or not _generation_request(response.url):
                return
            if response.ok:
                result["data"] = response.json()
            else:
                result["error"] = f"Suno generation returned HTTP {response.status}"

        page.route("**/api/generate/**", prepare)
        page.on("response", receive)
        page.get_by_role("textbox").first.fill(
            payload.get("gpt_description_prompt") or payload.get("prompt") or payload.get("tags") or "Instrumental music"
        )
        page.get_by_role("button", name="Create song", exact=True).click()
        deadline = time.monotonic() + 120
        verified = False
        while time.monotonic() < deadline:
            if "data" in result:
                return result["data"]
            if "error" in result:
                raise RuntimeError(result["error"])
            if not verified:
                verified = _click_verification(page)
            page.wait_for_timeout(1000)
        raise TimeoutError("Suno browser did not return generation clip ids; check the library before retrying")


def download_music(cookies, clip_id, output_path):
    output_path = Path(output_path)
    with _browser_page(cookies) as page:
        _open(page, f"https://suno.com/song/{clip_id}")
        page.get_by_role("button", name="More options", exact=True).first.click()
        page.get_by_text("Download", exact=True).click()
        mp3 = page.get_by_role("button", name="MP3", exact=True)
        mp3.wait_for(state="visible")
        if mp3.locator("svg").count() == 0:
            mp3.click()
        unlock = page.get_by_role("button", name="Unlock & Download", exact=True)
        action = unlock if unlock.count() else page.get_by_role("button", name="Download", exact=True)
        with page.expect_download(timeout=120000) as pending:
            action.click()
        download = pending.value
        failure = download.failure()
        if failure:
            raise RuntimeError(f"Suno MP3 download failed: {failure}")
        temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
        try:
            download.save_as(str(temporary))
            if not temporary.stat().st_size:
                raise RuntimeError("Downloaded Suno MP3 is empty")
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
