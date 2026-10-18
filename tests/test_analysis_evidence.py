import re

import pytest

from fitbit_air.analysis.evidence import load_registry


def test_bundled_registry_loads_with_sources_and_params():
    reg = load_registry()
    assert reg.has("plews2013_hrv")
    assert reg.source("plews2013_hrv").doi == "10.1007/s40279-013-0071-8"
    assert reg.param("hrv_band_halfwidth_sd") == 0.5
    assert all(reg.source(i).design_choice for i in reg.ids if i.startswith("design_"))


def test_missing_param_and_source_raise_clear_errors():
    reg = load_registry()
    with pytest.raises(KeyError, match="nope"):
        reg.param("nope")
    with pytest.raises(KeyError, match="nope"):
        reg.source("nope")


def test_duplicate_or_incomplete_sources_are_rejected():
    dup = '[[source]]\nid="a"\ncitation="x"\neffect="y"\n[[source]]\nid="a"\ncitation="x"\neffect="y"\n'
    with pytest.raises(ValueError, match="duplicate"):
        load_registry(dup)
    with pytest.raises(ValueError, match="citation"):
        load_registry('[[source]]\nid="a"\neffect="y"\n')


def _src(extra: str = "", id_: str = "a") -> str:
    return f'[[source]]\nid="{id_}"\ncitation="x"\neffect="y"\n{extra}'


def test_bundled_registry_is_cached():
    assert load_registry() is load_registry()


def test_ref_is_optional_and_loaded():
    reg = load_registry(_src('ref="Table 2"\n') + _src(id_="b"))
    assert reg.source("a").ref == "Table 2"
    assert reg.source("b").ref is None


@pytest.mark.parametrize(
    "params",
    ["x = true", 'x = "1"', "x = nan", "x = inf", "[params.x]\na = 1"],
)
def test_params_must_be_finite_numbers(params):
    text = params if params.startswith("[") else "[params]\n" + params
    with pytest.raises(ValueError, match="evidence param 'x' must be a finite number"):
        load_registry(text)


def test_int_and_float_params_are_accepted():
    reg = load_registry("[params]\na = 1\nb = 2.5\n")
    assert reg.param("a") == 1.0 and reg.param("b") == 2.5


def test_unknown_source_keys_are_rejected():
    with pytest.raises(ValueError, match="unknown keys.*desing_choice"):
        load_registry(_src("desing_choice = true\n"))


@pytest.mark.parametrize(
    "text",
    [
        '[[source]]\nid=""\ncitation="x"\neffect="y"\n',
        '[[source]]\nid="a"\ncitation=""\neffect="y"\n',
        '[[source]]\nid="a"\ncitation="x"\neffect=3\n',
        _src('design_choice = "yes"\n'),
        _src("used_for = \"x\"\n"),
        _src("used_for = [1]\n"),
        _src("doi = 1\n"),
        _src("ref = 1\n"),
    ],
)
def test_source_field_types_are_enforced(text):
    with pytest.raises(ValueError):
        load_registry(text)


def test_design_ids_must_be_flagged_as_design_choices():
    with pytest.raises(ValueError, match="design_choice"):
        load_registry(_src(id_="design_x"))
    reg = load_registry(_src("design_choice = true\n", id_="design_x"))
    assert reg.source("design_x").design_choice


def test_numbers_live_only_in_params_for_design_sources():
    reg = load_registry()
    for i in reg.ids:
        if i.startswith("design_") or i == "safety_thresholds":
            assert not re.search(r"(?<![A-Za-z])\d", reg.source(i).citation), i


from fitbit_air.analysis.evidence import Table


def test_table_kinds_interpolate_correctly():
    step = Table("step", ((0.0, 1.0), (150.0, 0.69), (300.0, 0.63)))
    assert step.value(-5) == 1.0 and step.value(0) == 1.0 and step.value(149.9) == 1.0
    assert step.value(150) == 0.69 and step.value(299) == 0.69 and step.value(10_000) == 0.63
    lin = Table("linear", ((0.0, 1.0), (30.0, 0.85), (60.0, 0.85), (140.0, 1.0)))
    assert lin.value(15) == pytest.approx(0.925) and lin.value(45) == 0.85 and lin.value(500) == 1.0
    loglin = Table("loglinear", ((2000.0, 1.0), (7000.0, 0.53)))
    assert loglin.value(1000) == 1.0 and loglin.value(9000) == 0.53
    assert loglin.value(4500) == pytest.approx(0.53 ** 0.5, rel=1e-6)  # geometric midpoint


def test_bundled_tables_present_and_match_verified_anchors():
    reg = load_registry()
    steps = reg.table("ding2025_steps")
    assert steps.kind == "loglinear" and steps.value(7000) == pytest.approx(0.53) and steps.value(12000) == pytest.approx(0.45)
    sri = reg.table("windred2024_sri")
    assert sri.value(70) == 1.0 and sri.value(81) == pytest.approx(0.75) and sri.value(90) == pytest.approx(0.70)
    assert reg.param("gompertz_b") == pytest.approx(0.087)
    assert reg.param("vo2_ref") == 46.5
    for sid in ("ding2025_steps", "kodama2009_crf", "zhang2016_rhr", "arem2015_pa", "momma2022_strength",
                "cappuccio2010_sleep", "gbmc2016_bmi", "pang2021_hr_to_age", "kaminsky2022_friend",
                "frandsen2025_spikes", "riegel1981", "uth2004_vo2max", "tanaka2001_hrmax", "karvonen1957"):
        assert reg.has(sid), sid


def test_table_validation():
    bad_kind = '[tables.x]\nkind = "cubic"\npoints = [[0, 1], [1, 2]]\n'
    with pytest.raises(ValueError, match="kind"):
        load_registry(bad_kind)
    unsorted = '[tables.x]\nkind = "step"\npoints = [[1, 1], [0, 2]]\n'
    with pytest.raises(ValueError, match="increasing"):
        load_registry(unsorted)
    nonpos = '[tables.x]\nkind = "loglinear"\npoints = [[0, 1], [1, 0]]\n'
    with pytest.raises(ValueError, match="positive"):
        load_registry(nonpos)
    with pytest.raises(KeyError, match="nope"):
        load_registry().table("nope")


def test_directly_constructed_tables_are_validated():
    from fitbit_air.analysis.evidence import Table
    with pytest.raises(ValueError, match="kind"):
        Table("cubic", ((0.0, 1.0), (1.0, 2.0)))
    with pytest.raises(ValueError, match="two points"):
        Table("step", ((0.0, 1.0),))
    with pytest.raises(ValueError, match="increasing"):
        Table("linear", ((1.0, 1.0), (0.0, 2.0)))
    with pytest.raises(ValueError, match="positive"):
        Table("loglinear", ((0.0, 1.0), (1.0, 0.0)))
    with pytest.raises(ValueError, match="finite"):
        Table("linear", ((0.0, 1.0), (1.0, float("nan"))))


def test_category_midpoint_tables_are_continuous_and_anchored():
    reg = load_registry()
    sleep = reg.table("cappuccio2010_sleep")
    assert sleep.kind == "loglinear"
    assert sleep.value(300) == pytest.approx(1.12) and sleep.value(420) == 1.00 and sleep.value(450) == 1.00
    assert sleep.value(600) == pytest.approx(1.30) and sleep.value(100) == pytest.approx(1.12)
    assert abs(sleep.value(359.9) - sleep.value(360)) < 1e-3  # no cliff at the 6 h category edge
    assert abs(sleep.value(539.9) - sleep.value(540)) < 1e-3  # nor at 9 h
    mvpa = reg.table("arem2015_mvpa")
    assert mvpa.kind == "loglinear" and mvpa.value(75) == pytest.approx(0.80) and mvpa.value(225) == pytest.approx(0.69)
    assert abs(mvpa.value(149.9) - mvpa.value(150)) < 1e-3 and mvpa.value(5000) == pytest.approx(0.69)
    bmi = reg.table("gbmc2016_bmi")
    assert bmi.kind == "loglinear" and bmi.value(32.5) == pytest.approx(1.45) and bmi.value(37.5) == pytest.approx(1.94)
    assert bmi.value(17) > 1.13 > bmi.value(21.25) == 1.0  # underweight carries excess risk too
    assert reg.has("gbmc2016_bmi") and not any(k in reg._params for k in ("bmi_hr_per_5", "bmi_floor"))

