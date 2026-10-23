import json
import sys
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from custom_tools.storybook import music_generator


@pytest.mark.parametrize("audio_url", [None, "https://studio-api.prod.suno.com/api/forbidden"])
def test_completed_clip_can_download_without_public_audio_url(monkeypatch, audio_url):
    clip = {"id": "ready-clip", "status": "complete", "audio_url": audio_url}
    monkeypatch.setattr(music_generator, "_get_suno_feed", lambda *a: {"clips": [clip]})
    result = music_generator._wait_for_suno_audio_url({}, ["ready-clip"], 0, 1)
    assert result[1:] == ("https://suno.com/song/ready-clip", "ready-clip")


def test_submission_uses_browser_instead_of_direct_generate_request(monkeypatch):
    seen = []
    browser = SimpleNamespace(submit_music=lambda cookies, payload: seen.append((cookies, payload)) or {"clips": [{"id": "new-clip"}]})
    monkeypatch.setitem(sys.modules, "custom_tools.storybook.suno_browser", browser)
    monkeypatch.setattr(music_generator.requests, "post", lambda *a, **kw: pytest.fail("Direct generation must not run"))
    payload = {"make_instrumental": True, "gpt_description_prompt": "piano"}
    result = music_generator._post_suno_generate({"cookies": {"__client": "refresh"}}, payload)
    assert result["clips"][0]["id"] == "new-clip"
    assert seen == [({"__client": "refresh"}, payload)]


def test_song_page_download_uses_browser(monkeypatch, tmp_path):
    seen = []
    target = tmp_path / "theme.mp3"

    def download(cookies, clip_id, output):
        seen.append((cookies, clip_id, output))
        output.write_bytes(b"mp3")

    monkeypatch.setenv("SUNO_COOKIE", "__client=refresh")
    monkeypatch.setitem(sys.modules, "custom_tools.storybook.suno_browser", SimpleNamespace(download_music=download))
    monkeypatch.setattr(music_generator.requests, "get", lambda *a, **kw: pytest.fail("Song page must not be downloaded as MP3"))
    music_generator._download_audio("https://suno.com/song/ready-clip", target)
    assert seen == [({"__client": "refresh"}, "ready-clip", target)]
    assert target.read_bytes() == b"mp3"


@pytest.mark.parametrize("url", [
    "https://studio-api.prod.suno.com/api/generate/v2-web/",
    "https://suno.com/a/api/generate/v2-web/",
])
def test_browser_preserves_verification_token_and_requested_music_settings(monkeypatch, url):
    from custom_tools.storybook import suno_browser

    sent = []
    callbacks = {}
    browser_body = {"token": "fresh-browser-token", "metadata": {"session": "browser"}}

    def click():
        request = SimpleNamespace(
            method="POST", url=url,
            post_data_json=browser_body,
        )
        callbacks["route"](SimpleNamespace(request=request, continue_=lambda **kw: sent.append(json.loads(kw["post_data"]))))
        callbacks["response"](SimpleNamespace(
            request=request, url=request.url, ok=True, json=lambda: {"clips": [{"id": "paid-clip"}]},
        ))

    control = SimpleNamespace(fill=lambda text: None, click=click)
    control.first = control
    page = SimpleNamespace(
        route=lambda pattern, callback: callbacks.update(route=callback),
        on=lambda event, callback: callbacks.update({event: callback}),
        get_by_role=lambda *args, **kw: control,
    )

    @contextmanager
    def fake_page(cookies):
        yield page

    monkeypatch.setattr(suno_browser, "_browser_page", fake_page)
    monkeypatch.setattr(suno_browser, "_open", lambda *a: None)
    payload = {"mv": "chirp-hawk", "make_instrumental": True, "token": None, "prompt": "lyrics", "tags": "piano"}
    assert suno_browser.submit_music({}, payload)["clips"][0]["id"] == "paid-clip"
    assert len(sent) == 1
    assert sent[0]["token"] == "fresh-browser-token"
    assert sent[0]["mv"] == "chirp-hawk"
    assert sent[0]["make_instrumental"] is True
    assert sent[0]["metadata"] == {"session": "browser", "create_mode": "custom"}


@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize("locked", [True, False])
def test_mp3_download_keeps_selected_format(monkeypatch, tmp_path, selected, locked):
    from custom_tools.storybook import suno_browser

    clicks = []

    class Control:
        def __init__(self, name):
            self.name = name
            self.first = self

        def count(self):
            return int(locked) if self.name == "Unlock & Download" else int(selected)

        def click(self):
            clicks.append(self.name)

        def wait_for(self, **kwargs):
            pass

        def locator(self, name):
            return self

    @contextmanager
    def pending_download(**kwargs):
        yield SimpleNamespace(value=SimpleNamespace(
            failure=lambda: None, save_as=lambda path: Path(path).write_bytes(b"MP3 audio"),
        ))

    page = SimpleNamespace(
        get_by_role=lambda role, name, **kw: Control(name),
        get_by_text=lambda name, **kw: Control(name), expect_download=pending_download,
    )

    @contextmanager
    def fake_page(cookies):
        yield page

    monkeypatch.setattr(suno_browser, "_browser_page", fake_page)
    monkeypatch.setattr(suno_browser, "_open", lambda *args: None)
    target = tmp_path / "track.mp3"
    suno_browser.download_music({}, "clip-id", target)
    assert target.read_bytes() == b"MP3 audio"
    assert clicks.count("MP3") == (0 if selected else 1)
    assert clicks[-1] == ("Unlock & Download" if locked else "Download")


def test_browser_download_failure_resumes_paid_clip(monkeypatch, tmp_path):
    submissions = []
    attempts = []
    monkeypatch.setenv("SUNO_COOKIE", "__client=refresh")
    monkeypatch.setattr(music_generator, "_load_env_file", lambda: None)
    monkeypatch.setattr(music_generator, "_authenticate", lambda cookie: {"cookies": {"__client": "refresh"}})
    monkeypatch.setattr(music_generator, "_get_suno_feed", lambda *args: {
        "clips": [{"id": "paid-clip", "status": "complete", "audio_url": "https://studio-api.prod.suno.com/api/forbidden"}],
    })

    def submit(*args):
        submissions.append(args)
        return {"clips": [{"id": "paid-clip"}]}

    def download(cookies, clip_id, output):
        attempts.append(clip_id)
        if len(attempts) == 1:
            raise TimeoutError("Download temporarily unavailable")
        output.write_bytes(b"mp3")

    monkeypatch.setitem(sys.modules, "custom_tools.storybook.suno_browser", SimpleNamespace(
        submit_music=submit, download_music=download,
    ))
    args = (tmp_path, "theme", "piano", tmp_path / "theme.mp3", True, True, 1, 5, False)
    with pytest.raises(TimeoutError):
        music_generator._generate_single_track(*args)
    state = music_generator._read_submitted_state(tmp_path, "theme")
    assert state["clip_ids"] == ["paid-clip"]
    assert music_generator._generate_single_track(*args).read_bytes() == b"mp3"
    assert len(submissions) == 1
    assert attempts == ["paid-clip", "paid-clip"]
