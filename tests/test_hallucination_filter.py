import pytest
from soulogos_session.bot import _is_hallucination, _clean_lines_for_llm


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "...",
    "!?",
    "---",
    "Thank you for watching",
    "Thanks for watching",
    "Thank you for joining",
    "Don't forget to like and subscribe",
    "Please like and subscribe",
    "Subscribe",
    "Like and subscribe",
    "D&D, dungeons and dragons",
    "TTRPG dungeon master",
    "DnD campaign tips",
    "TTRTRAGON HUNTERS BUYING",
    "okay",
    "um",
    "uh",
    "bye.",
    "yeah",
    "Thank you for watching, like share and subscribe",
])
def test_is_hallucination_true(text):
    assert _is_hallucination(text) is True


@pytest.mark.parametrize("text", [
    "I don't plan on letting him out",
    "Thank you for joining me in this building",
    "You don't find anything of any value",
    "Spell books looking for",
    "And I'm all about loose ends",
    "No, we're not looking for value there",
])
def test_is_hallucination_false(text):
    assert _is_hallucination(text) is False


def test_clean_lines_empty():
    assert _clean_lines_for_llm([]) == []


def test_clean_lines_all_clean():
    lines = [
        {"text": "I cast fireball.", "display_name": "Thalindra"},
        {"text": "Roll for damage.", "display_name": "DM"},
    ]
    assert _clean_lines_for_llm(lines) == lines


def test_clean_lines_all_hallucinations():
    lines = [
        {"text": "Thank you for watching", "display_name": "A"},
        {"text": "...", "display_name": "B"},
        {"text": "Subscribe", "display_name": "C"},
    ]
    assert _clean_lines_for_llm(lines) == []


def test_clean_lines_mixed():
    lines = [
        {"text": "I move toward the door.", "display_name": "Riven"},
        {"text": "um", "display_name": "Riven"},
        {"text": "What do I see?", "display_name": "Riven"},
        {"text": "okay", "display_name": "DM"},
    ]
    result = _clean_lines_for_llm(lines)
    assert len(result) == 2
    assert result[0]["text"] == "I move toward the door."
    assert result[1]["text"] == "What do I see?"


def test_clean_lines_missing_text_excluded():
    lines = [
        {"display_name": "A"},
        {"text": "Valid line.", "display_name": "B"},
    ]
    result = _clean_lines_for_llm(lines)
    assert len(result) == 1
    assert result[0]["text"] == "Valid line."


def test_clean_lines_empty_text_excluded():
    lines = [
        {"text": "", "display_name": "A"},
        {"text": "Present and accounted for.", "display_name": "B"},
    ]
    result = _clean_lines_for_llm(lines)
    assert len(result) == 1
    assert result[0]["text"] == "Present and accounted for."


def test_clean_lines_preserves_all_fields():
    line = {
        "timestamp": "2026-06-30T00:00:00+00:00",
        "display_name": "Thalindra",
        "text": "I cast fireball.",
        "confidence": 0.92,
    }
    result = _clean_lines_for_llm([line])
    assert result == [line]
