import math
import tomllib

import pytest

from fitbit_air.analysis.profile import Profile, load_profile, profile_path, save_profile, set_value
from fitbit_air.config import Paths


def test_missing_profile_is_created_with_defaults(tmp_path):
    path = tmp_path / "data" / "profile.toml"
    p = load_profile(path)
    assert p == Profile()
    assert p.sex == "unspecified" and p.race_date is None and p.sleep_need_h == 7.5
    written = tomllib.loads(path.read_text())
    assert "race_distance_km" not in written and written["sleep_need_h"] == 7.5
    assert written["plugins"] == {"enabled": False}


def test_set_value_converts_types_and_persists(tmp_path):
    path = tmp_path / "profile.toml"
    set_value(path, "sleep_need_h", "8")
    set_value(path, "hrmax_override", "195")
    set_value(path, "goals", "sleep, running_10k")
    p = load_profile(path)
    assert p.sleep_need_h == 8.0 and p.hrmax_override == 195 and p.goals == ["sleep", "running_10k"]
    set_value(path, "hrmax_override", "none")
    assert load_profile(path).hrmax_override is None


def test_invalid_values_are_rejected(tmp_path):
    path = tmp_path / "profile.toml"
    with pytest.raises(ValueError, match="sex"):
        set_value(path, "sex", "robot")
    with pytest.raises(ValueError, match="unknown key"):
        set_value(path, "colour", "blue")
    with pytest.raises(ValueError):
        set_value(path, "race_date", "07/02/2027")


def test_unknown_keys_in_file_are_rejected(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text('sex = "male"\nfavourite = 1\n')
    with pytest.raises(ValueError, match="unknown profile keys"):
        load_profile(path)


def test_save_skips_unset_optionals(tmp_path):
    path = tmp_path / "profile.toml"
    save_profile(path, Profile())
    assert "hrmax_override" not in tomllib.loads(path.read_text())


def test_native_toml_date_loads(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text("race_date = 2027-02-07\n")
    assert load_profile(path).race_date == "2027-02-07"


def test_int_values_normalise_to_float_and_integral_float_to_int(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text("sleep_need_h = 8\nrace_distance_km = 21\nhrmax_override = 190.0\n")
    p = load_profile(path)
    assert p.sleep_need_h == 8.0 and isinstance(p.sleep_need_h, float)
    assert isinstance(p.race_distance_km, float)
    assert p.hrmax_override == 190 and isinstance(p.hrmax_override, int)


@pytest.mark.parametrize(
    "line",
    [
        'sleep_need_h = "8"',
        "sleep_need_h = true",
        "hrmax_override = 190.5",
        "hrmax_override = true",
        "sex = 1",
        "race_date = 20270207",
        'goals = "sleep"',
        "goals = [1, 2]",
    ],
)
def test_wrong_types_in_file_raise_value_error(tmp_path, line):
    path = tmp_path / "profile.toml"
    path.write_text(line + "\n")
    with pytest.raises(ValueError):
        load_profile(path)


@pytest.mark.parametrize("dist", ["nan", "inf", "0", "-5", "100.5"])
def test_race_distance_must_be_finite_and_in_range(tmp_path, dist):
    with pytest.raises(ValueError):
        set_value(tmp_path / "profile.toml", "race_distance_km", dist)
    assert set_value(tmp_path / "ok.toml", "race_distance_km", "100").race_distance_km == 100.0


def test_race_date_must_round_trip(tmp_path):
    with pytest.raises(ValueError):
        set_value(tmp_path / "profile.toml", "race_date", "20270207")


def test_control_characters_are_rejected():
    with pytest.raises(ValueError, match="control"):
        Profile(goals=["a\nb"]).validate()
    with pytest.raises(ValueError):
        Profile(sex="male\x00").validate()
    with pytest.raises(ValueError):
        Profile(race_date="2027-02-07\x7f").validate()


def test_set_value_lowercases_sex_and_wraps_conversion_errors(tmp_path):
    path = tmp_path / "profile.toml"
    assert set_value(path, "sex", " Female ").sex == "female"
    with pytest.raises(ValueError, match="sleep_need_h"):
        set_value(path, "sleep_need_h", "abc")
    with pytest.raises(ValueError, match="hrmax_override"):
        set_value(path, "hrmax_override", "1.5")


def test_awkward_strings_round_trip(tmp_path):
    path = tmp_path / "profile.toml"
    p = Profile(goals=['say "hi"', "back\\slash", "naïve ✓"], height_m=1.8, hrmax_override=190)
    save_profile(path, p)
    assert load_profile(path) == p


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "profile.toml"
    save_profile(path, Profile())
    save_profile(path, Profile(sleep_need_h=8.0))
    assert [f.name for f in tmp_path.iterdir()] == ["profile.toml"]
    assert "profile set" in path.read_text().splitlines()[0]
    assert math.isclose(load_profile(path).sleep_need_h, 8.0)


def test_defaults_are_neutral():
    p = Profile()
    assert (p.sex, p.race_date, p.race_distance_km, p.goals, p.units, p.plugins_enabled) == \
        ("unspecified", None, None, [], "metric", False)
    assert p.has_race is False


def test_race_needs_both_fields_units_and_timezone_validated():
    for bad in (dict(units="furlongs"), dict(timezone="Mars/Olympus"), dict(race_date="06/01/2027")):
        with pytest.raises(ValueError):
            Profile(**bad).validate()
    ok = Profile(race_date="2027-06-01", race_distance_km=10.0, timezone="Europe/London", units="imperial")
    ok.validate()
    assert ok.has_race
    half = Profile(race_date="2027-06-01")
    half.validate()  # allowed while being set up; it just means no race plan yet
    assert half.has_race is False


def test_plugins_table_round_trip(tmp_path):
    path = tmp_path / "config" / "profile.toml"
    save_profile(path, Profile(plugins_enabled=True))
    assert "[plugins]\nenabled = true" in path.read_text()
    assert load_profile(path).plugins_enabled is True
    path.write_text('[plugins]\nenabled = true\nbogus = 1\n')
    with pytest.raises(ValueError, match="plugins"):
        load_profile(path)


def test_profile_path_prefers_config_dir_then_legacy(tmp_path):
    paths = Paths(tmp_path)
    assert profile_path(paths) == tmp_path / "config" / "profile.toml"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "profile.toml").write_text('sex = "female"\n')
    assert profile_path(paths) == tmp_path / "data" / "profile.toml"  # legacy file read until migrated
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.toml").write_text('sex = "male"\n')
    assert profile_path(paths) == tmp_path / "config" / "profile.toml"


def test_set_value_clears_race_and_sets_new_keys(tmp_path):
    path = tmp_path / "config" / "profile.toml"
    set_value(path, "race_date", "2027-06-01")
    p = set_value(path, "race_distance_km", "21.1")
    assert p.has_race
    set_value(path, "race_date", "none")
    p = set_value(path, "race_distance_km", "none")
    assert not p.has_race
    assert set_value(path, "units", "imperial").units == "imperial"
    assert set_value(path, "plugins_enabled", "true").plugins_enabled is True


def test_top_level_plugins_enabled_key_is_rejected(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text("plugins_enabled = true\n")
    with pytest.raises(ValueError, match="isn't a top-level key"):
        load_profile(path)
    with pytest.raises(ValueError, match="true or false"):
        set_value(tmp_path / "other.toml", "plugins_enabled", "maybe")


def test_validate_rejects_non_bool_plugins_enabled_and_wrong_case_timezone():
    with pytest.raises(ValueError, match="plugins_enabled"):
        Profile(plugins_enabled="yes").validate()
    with pytest.raises(ValueError, match="timezone"):
        Profile(timezone="europe/london").validate()


def test_keys_written_after_plugins_get_a_helpful_error(tmp_path):
    import pytest

    from fitbit_air.analysis.profile import load_profile

    path = tmp_path / "profile.toml"
    path.write_text('sex = "male"\n[plugins]\nenabled = false\nsleep_need_h = 8\nunits = "metric"\n')
    with pytest.raises(ValueError, match=r"keys written after \[plugins\] belong to that table; move sleep_need_h, "
                                         r"units above the \[plugins\] line"):
        load_profile(path)


def test_goals_are_capped_in_length_and_number():
    import pytest

    from fitbit_air.analysis.profile import Profile

    Profile(goals=["g" * 60] * 10).validate()
    with pytest.raises(ValueError, match="at most 60 characters"):
        Profile(goals=["g" * 61]).validate()
    with pytest.raises(ValueError, match="at most 10 goals"):
        Profile(goals=[f"g{i}" for i in range(11)]).validate()
