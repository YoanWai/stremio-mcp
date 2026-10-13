import json
import random
import re
import shutil
from dataclasses import replace

import httpx
import pytest

from stremio_mcp import subtitle_sync

PLAYING = "The.Show.S05E03.1080p.BluRay.x264-ROVERS.mkv"
SOURCE = "https://subs.example"
INSTALLED_SOURCES = subtitle_sync._subtitle_sources


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitle_sync, "settings", replace(subtitle_sync.settings, state_dir=tmp_path)
    )
    monkeypatch.setattr(subtitle_sync, "_subtitle_sources", lambda content_type, video_id: [SOURCE])


def srt(cues):
    blocks = []
    for index, (start, end, text) in enumerate(cues, 1):
        blocks.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
    return "\n".join(blocks)


def stamp(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def cue_starts(text):
    return [
        int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        for h, m, s, ms in re.findall(r"^(\d+):(\d+):(\d+),(\d+) -->", text, re.M)
    ]


def listed(lang, path, release, field="id"):
    return {"lang": lang, "url": f"{SOURCE}{path}", field: release}


def responder(listing, files, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(str(request.url))
        if request.url.path.startswith("/subtitles/"):
            return httpx.Response(200, json={"subtitles": listing})
        return httpx.Response(200, content=files[request.url.path])

    return handler


def client_for(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def serve(listing, files, requests=None):
    return client_for(responder(listing, files, requests))


@pytest.mark.parametrize(
    "name,group,source",
    [
        ("The.Show.S05E03.1080p.BluRay.x264-ROVERS.mkv", "rovers", "bluray"),
        ("[KTUVIT]The.Show.S05E03.720p.WEB-DL.DD5.1.H.264-Cyphanix", "cyphanix", "web"),
        ("The.Show.S05E03.INTERNAL.HDTV.x264-BATV", "batv", "hdtv"),
        ("The.Show.S05E03.BDRip.x264-DEMAND", "demand", "bluray"),
        ("The.Show.S05E03.1080p.TrueHD.7.1.AVC.REMUX-FraMeSToR.mkv", "framestor", "bluray"),
        ("The Show (2010) - S05E03 - A Title (1080p BluRay x265 Silence).mkv", "", "bluray"),
        ("The Show - 5x03 - A Title.mkv", "", ""),
    ],
)
def test_release_traits(name, group, source):
    assert subtitle_sync.release_traits(name) == (group, source)


def test_rank_prefers_the_same_group_then_the_same_source():
    candidates = [
        subtitle_sync.Candidate("heb", "u1", "The.Show.S05E03.HDTV.x264-KILLERS"),
        subtitle_sync.Candidate("heb", "u2", "The.Show.S05E03.720p.WEB-DL.H.264-Cyphanix"),
        subtitle_sync.Candidate("heb", "u3", "The.Show.S05E03.720p.BluRay.x264-DEMAND"),
        subtitle_sync.Candidate("heb", "u4", "The.Show.S05E03.1080p.BluRay.x264-ROVERS"),
    ]
    ranked = subtitle_sync.rank(candidates, PLAYING)
    assert [candidate.url for candidate in ranked] == ["u4", "u3", "u2", "u1"]


def test_drop_cues_before_start_removes_cues_cut_from_this_release():
    aligned = (
        "1\n00:00:-23,-451 --> 00:00:-20,-43\nrecap\n\n"
        "2\n00:00:-1,-500 --> 00:00:01,200\nstraddles the start\n\n"
        "3\n00:00:05,000 --> 00:00:07,000\nkept\n"
    )
    cleaned = subtitle_sync.drop_cues_before_start(aligned)
    assert "recap" not in cleaned
    assert cleaned.startswith("1\n00:00:00,000 --> 00:00:01,200\nstraddles the start")
    assert "2\n00:00:05,000 --> 00:00:07,000\nkept" in cleaned


def test_no_filename_means_no_synced_subtitle(monkeypatch):
    monkeypatch.setattr(subtitle_sync, "_http_client", serve([], {}))
    assert subtitle_sync.synced_subtitle("series", "tt1:5:3", {}) is None


def test_serves_the_matching_target_subtitle_untouched(monkeypatch):
    listing = [
        listed("heb", "/hdtv.srt", "[KTUVIT]The.Show.S05E03.HDTV.x264-KILLERS"),
        listed("heb", "/rovers.srt", "[KTUVIT]The.Show.S05E03.1080p.BluRay.x264-ROVERS"),
        listed("eng", "/eng.srt", "The.Show.S05E03.720p.WEB-DL-NTb", field="movieReleaseName"),
    ]
    files = {"/hdtv.srt": b"1\n00:00:01,000 --> 00:00:02,000\nhdtv\n",
             "/rovers.srt": b"1\n00:00:01,000 --> 00:00:02,000\nrovers\n",
             "/eng.srt": b"1\n00:00:01,000 --> 00:00:02,000\neng\n"}
    monkeypatch.setattr(subtitle_sync, "_http_client", serve(listing, files))
    monkeypatch.setattr(subtitle_sync, "align", lambda reference, subtitle: pytest.fail("aligned"))

    path = subtitle_sync.synced_subtitle("series", "tt1:5:3", {"filename": PLAYING})

    assert path.read_bytes() == files["/rovers.srt"]


def test_aligns_to_a_reference_cut_for_the_playing_release(monkeypatch):
    listing = [
        listed("heb", "/hdtv.srt", "The.Show.S05E03.HDTV.x264-KILLERS", field="movieReleaseName"),
        listed("eng", "/eng.srt", "The.Show.S05E03.1080p.BluRay.x264-ROVERS", field="movieReleaseName"),
    ]
    files = {"/hdtv.srt": b"1\n00:00:01,000 --> 00:00:02,000\nhdtv\n",
             "/eng.srt": b"1\n00:00:01,000 --> 00:00:02,000\neng\n"}
    calls = []
    monkeypatch.setattr(subtitle_sync, "_http_client", serve(listing, files))
    monkeypatch.setattr(
        subtitle_sync, "align", lambda reference, subtitle: calls.append((reference, subtitle)) or b"aligned"
    )

    path = subtitle_sync.synced_subtitle("series", "tt1:5:3", {"filename": PLAYING})

    assert calls == [(files["/eng.srt"], files["/hdtv.srt"])]
    assert path.read_bytes() == b"aligned"


def test_skips_downloads_that_are_not_subtitles(monkeypatch):
    listing = [
        listed("heb", "/broken.srt", "[KTUVIT]The.Show.S05E03.1080p.BluRay.x264-ROVERS"),
        listed("heb", "/good.srt", "[KTUVIT]The.Show.S05E03.720p.BluRay.x264-DEMAND"),
    ]
    files = {"/broken.srt": b"Error code: 502", "/good.srt": b"1\n00:00:01,000 --> 00:00:02,000\ngood\n"}
    monkeypatch.setattr(subtitle_sync, "_http_client", serve(listing, files))

    path = subtitle_sync.synced_subtitle("series", "tt1:5:3", {"filename": PLAYING})

    assert path.read_bytes() == files["/good.srt"]


def test_forwards_the_extras_and_caches_the_result(monkeypatch):
    listing = [listed("heb", "/a.srt", "The.Show.S05E03.BluRay-ROVERS")]
    files = {"/a.srt": b"1\n00:00:01,000 --> 00:00:02,000\na\n"}
    requests = []
    monkeypatch.setattr(subtitle_sync, "_http_client", serve(listing, files, requests))
    extra = {"filename": "The Show (2010) - S05E03.mkv", "videoSize": "42"}

    first = subtitle_sync.synced_subtitle("series", "tt1:5:3", extra)
    second = subtitle_sync.synced_subtitle("series", "tt1:5:3", extra)

    assert first == second
    assert requests[0] == (
        f"{SOURCE}/subtitles/series/tt1%3A5%3A3/"
        "filename=The%20Show%20%282010%29%20-%20S05E03.mkv&videoSize=42.json"
    )
    assert len(requests) == 2


def test_a_failing_source_does_not_hide_the_others(monkeypatch):
    other = "https://down.example"
    monkeypatch.setattr(subtitle_sync, "_subtitle_sources", lambda content_type, video_id: [other, SOURCE])
    listing = [listed("heb", "/a.srt", "The.Show.S05E03.BluRay-ROVERS")]
    files = {"/a.srt": b"1\n00:00:01,000 --> 00:00:02,000\na\n"}
    healthy = responder(listing, files)

    def handler(request):
        if request.url.host == "down.example":
            return httpx.Response(502, text="Bad Gateway")
        return healthy(request)

    monkeypatch.setattr(subtitle_sync, "_http_client", client_for(handler))

    path = subtitle_sync.synced_subtitle("series", "tt1:5:3", {"filename": PLAYING})

    assert path.read_bytes() == files["/a.srt"]


def test_subtitle_sources_are_the_installed_subtitle_addons(monkeypatch):
    collection = [
        {"transportUrl": "https://ours.example/manifest.json",
         "manifest": {"id": "org.stremio.mcp-subtitles", "resources": ["subtitles"], "types": ["series"]}},
        {"transportUrl": "https://opensubs.example/manifest.json",
         "manifest": {"id": "os", "resources": ["subtitles"], "types": ["movie", "series"]}},
        {"transportUrl": "https://legacy.example/stremio/v1",
         "manifest": {"id": "legacy", "resources": ["subtitles"], "types": ["series"]}},
        {"transportUrl": "https://streams.example/manifest.json",
         "manifest": {"id": "streams", "resources": ["stream"], "types": ["series"]}},
    ]

    def handler(request):
        assert json.loads(request.content)["type"] == "AddonCollectionGet"
        return httpx.Response(200, json={"result": {"addons": collection}})

    monkeypatch.setattr(subtitle_sync.api, "current_key", lambda: "key")
    monkeypatch.setattr(subtitle_sync, "_http_client", client_for(handler))

    assert INSTALLED_SOURCES("series", "tt1:5:3") == ["https://opensubs.example"]


@pytest.mark.skipif(shutil.which("alass-cli") is None, reason="alass-cli is not installed")
def test_align_undoes_offsets_that_change_at_each_break():
    rng = random.Random(7)
    times, clock = [], 30.0
    while clock < 1800:
        times.append(clock)
        clock += rng.uniform(1.5, 9.0)
    reference = srt([(start, start + 1.4, f"reference {index}") for index, start in enumerate(times)])

    def shifted(start):
        if start < 600:
            return start + 23.0
        if start < 1200:
            return start + 20.0
        return start + 15.0

    recap = [(1.0, 3.0, "recap one"), (3.5, 5.0, "recap two")]
    drifting = srt(recap + [(shifted(start), shifted(start) + 1.4, f"line {index}")
                            for index, start in enumerate(times)])

    aligned = subtitle_sync.align(reference.encode(), drifting.encode()).decode()

    assert "recap" not in aligned
    errors = [abs(got - want) for got, want in zip(cue_starts(aligned), times, strict=True)]
    assert sum(error < 0.3 for error in errors) / len(errors) > 0.95
