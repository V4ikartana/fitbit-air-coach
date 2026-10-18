import pytest

from fitbit_air.coach.journal import (MAX_TEXT, ValidationError, validate_journal, validate_note,
                                      validate_note_update)


def test_journal_accepts_and_normalises():
    assert validate_journal({"feeling": 4, "illness": True, "alcohol_units": 2, "notes": "  ok  ", "energy": None}) \
        == {"feeling": 4, "illness": 1, "alcohol_units": 2.0, "notes": "ok"}


@pytest.mark.parametrize("field,value", [("feeling", 0), ("feeling", 6), ("energy", 2.5), ("soreness", 11),
                                         ("session_rpe", -1), ("alcohol_units", 51), ("feeling", True),
                                         ("illness", "yes")])
def test_journal_rejects_out_of_range_naming_the_field(field, value):
    with pytest.raises(ValueError, match=field):
        validate_journal({field: value})


def test_journal_needs_a_field_and_caps_notes():
    with pytest.raises(ValueError, match="at least one"):
        validate_journal({"feeling": None, "notes": "   "})
    with pytest.raises(ValueError, match="notes"):
        validate_journal({"notes": "x" * (MAX_TEXT + 1)})
    with pytest.raises(ValueError, match="mood"):
        validate_journal({"mood": 3})


def test_note_validation():
    assert validate_note("plan", " easy 3 km ", "2026-10-18") == ("plan", "easy 3 km", "2026-10-18")
    assert validate_note("target", "3 runs", None) == ("target", "3 runs", None)
    for kind, text, review in (("goal", "x", None), ("plan", "", None), ("plan", "x", "18/10/2026"),
                               ("plan", "x" * (MAX_TEXT + 1), None)):
        with pytest.raises(ValueError):
            validate_note(kind, text, review)


def test_note_update_validation():
    assert validate_note_update(status="done") == {"status": "done"}
    assert validate_note_update(text=" new ", review_on="2026-10-20") == {"text": "new", "review_on": "2026-10-20"}
    with pytest.raises(ValueError, match="status"):
        validate_note_update(status="finished")
    with pytest.raises(ValueError, match="nothing"):
        validate_note_update()


def test_validation_error_is_a_value_error_and_text_must_be_str():
    assert issubclass(ValidationError, ValueError)
    with pytest.raises(ValidationError, match="notes must be text"):
        validate_journal({"notes": 5})
    with pytest.raises(ValidationError, match="text must be text"):
        validate_note("plan", ["x"], None)
    with pytest.raises(ValidationError, match="text must be text"):
        validate_note_update(text=7)
    for call in (lambda: validate_journal({"feeling": 9}), lambda: validate_note("goal", "x", None),
                 lambda: validate_note_update()):
        with pytest.raises(ValidationError):
            call()
