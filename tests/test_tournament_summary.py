"""Турнирные сообщения и итоговая таблица."""
import io

from PIL import Image

from app import tournament_summary as summary


def _state():
    return {
        "players": ["Илья", "Марго", "Анна"], "competitive": True,
        "schedule": [{"courts": [[0, 1], [0, 2]]}],
        "scores": {"0-0": {"a": 11, "b": 2}, "0-1": {"a": 7, "b": 5}},
    }


def test_summary_and_standings_match_tournament_rules():
    state = _state()
    assert not summary.complete(state)
    text = summary.message(state, "Турнир")
    assert "Раунд 1" in text and "Илья — Марго  ·  11 : 2" in text
    assert "Илья — Анна  ·  7 : 5" in text
    rows = summary.standings(state)
    assert [(r["name"], r["wins"], r["losses"]) for r in rows] == [("Илья", 1, 0), ("Анна", 0, 0), ("Марго", 0, 1)]
    state["scores"]["0-1"] = {"a": 8, "b": 11}
    assert summary.complete(state)
    picture = Image.open(io.BytesIO(summary.image(state)))
    assert picture.format == "PNG" and picture.width == 800


def test_summary_substitutions_playoff_and_safe_text():
    state = _state()
    state["players"][0] = "Новый <игрок>"
    state["past"] = {"0": [{"from": 0, "to": 1, "name": "Илья & Ко"}]}
    state["scores"]["0-1"] = {"a": 8, "b": 11}
    state["po"] = {"rounds": [[{"a": 0, "b": 1}]]}
    assert not summary.complete(state)
    assert "Илья &amp; Ко" in summary.message(state, "Турнир")
    state["scores"]["p0-0"] = {"a": 0, "b": 0, "end": {"winner": "b"}}
    assert summary.complete(state)
    assert "Плей-офф" in summary.message(state, "Турнир")
    assert all("Ожидается" not in row["name"] for row in summary.standings(state))
    state["po"]["rounds"][0][0]["a"] = None
    assert not summary.complete(state)


def test_summary_long_messages_and_missing_data():
    state = {"players": ["A" * 80, "B" * 80], "schedule": [{"courts": [[0, 1]] * 90}], "scores": {}}
    assert "Остальные матчи" in summary.message(state, "Турнир")
    assert len(summary.message(state, "Турнир")) <= 4096
    assert not summary.complete(state)
    assert summary.matches({"players": [], "schedule": [{"courts": [[0, 1]]}], "scores": {}})[0][1] == "Ожидается игрок"
    assert not summary.complete({"players": ["a", "b"], "schedule": [{"courts": [[0, 0]]}], "scores": {}})


def test_malformed_states_do_not_allow_premature_finish():
    base = _state()
    base["scores"]["0-1"] = {"a": 11, "b": 4}
    assert not summary.complete({"players": [], "schedule": [], "scores": {}})
    assert not summary.complete({**base, "schedule": [None]})
    assert not summary.complete({**base, "schedule": [{"courts": [None]}]})
    assert not summary.complete({**base, "po": {"rounds": []}})
    assert not summary.complete({**base, "po": {"rounds": [[]]}})
    broken = {**base, "schedule": [None, {"courts": [None, [0, 1]]}], "scores": {"1-1": "invalid"}, "po": {"rounds": [None, [None]]}}
    assert len(summary.matches(broken)) == 1
    assert summary.matches(broken)[0][3:] == (None, None, None)
    assert not summary.complete(broken)
    assert summary.image({**base, "players": ["Длинное имя игрока " * 8, "Игрок", "Третий"]}).startswith(b"\x89PNG")
