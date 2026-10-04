from core.autopilot.script import similarity, validate_episode


def make(short_words=130, tiktok_words=195, seg_words=100, hook="A city celebrates"):
    w = lambda n, seed: " ".join(f"word{(i * seed) % 97}" for i in range(n))
    return {"hook": hook, "title_short": "A city celebrates its team", "segment_title": "Series win",
            "script_short": w(short_words, 3), "script_tiktok": w(tiktok_words, 7),
            "script_segment": w(seg_words, 11), "description": "Footage of a parade."}


TARGETS = {"script_short": 130, "script_tiktok": 195, "script_segment": 100}


def test_valid_script_passes():
    assert validate_episode(make(), TARGETS, []) == []


def test_length_problems():
    problems = validate_episode(make(short_words=40), TARGETS, [])
    assert any("script_short" in p for p in problems)


def test_repeated_hook_and_similar_script_rejected():
    ep = make()
    recent = [{"hook": "a city CELEBRATES", "script_short": ep["script_short"]}]
    problems = validate_episode(ep, TARGETS, recent)
    assert "hook repeats a recent video" in problems
    assert "script too similar to a recent video" in problems


def test_sensational_hook_rejected():
    assert "sensational hook/title" in validate_episode(make(hook="The assassination nobody saw"), TARGETS, [])


def test_similarity():
    assert similarity("the quick brown fox", "the quick brown fox") == 1.0
    assert similarity("the quick brown fox", "completely different words here") < 0.3
