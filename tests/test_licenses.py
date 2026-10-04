import datetime as dt

from core.autopilot.sources import SENSITIVE, candidate_score, choose_video_file, classify_license


def test_allowed_licenses():
    assert classify_license("http://creativecommons.org/licenses/publicdomain/")[0]
    assert classify_license("https://creativecommons.org/publicdomain/mark/1.0/")[0]
    assert classify_license("http://creativecommons.org/publicdomain/zero/1.0/")[0]
    ok, name, attr = classify_license("http://creativecommons.org/licenses/by/4.0/")
    assert ok and attr and name.startswith("CC BY")


def test_rejected_licenses():
    for url in ("", None, "http://creativecommons.org/licenses/by-nc/3.0/",
                "http://creativecommons.org/licenses/by-nd/4.0/",
                "http://creativecommons.org/licenses/by-sa/4.0/",
                "http://creativecommons.org/licenses/by-nc-sa/2.0/",
                "https://example.com/all-rights-reserved"):
        assert not classify_license(url)[0], url


def test_sensitive_topics_filtered():
    assert SENSITIVE.search("1934-10-17 King Alexander Assassination")
    assert SENSITIVE.search("Nazis Face War Crime Evidence")
    assert not SENSITIVE.search("West Coast Hails First World Series")


def test_this_week_in_history_ranking():
    today = dt.date(2026, 10, 3)
    assert candidate_score("1959-10-05_World_Series", today) > candidate_score("1941-03-01_Other", today)
    assert candidate_score("no_date_here", today) == 0


def test_choose_video_file_prefers_h264_resolution():
    files = [
        {"name": "a.mpeg", "height": "480", "length": "60", "size": "9"},
        {"name": "a_512kb.mp4", "height": "240", "length": "60", "size": "5"},
        {"name": "a.mp4", "height": "480", "length": "60", "size": "7"},
    ]
    assert choose_video_file(files)["name"] == "a.mp4"
    assert choose_video_file([{"name": "x.ogv"}]) is None
