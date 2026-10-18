# The science

Every score, finding and limit in fitbit-air comes from a cited study, a guideline, or a design choice that is
labelled as one. This page explains how that is recorded, the principles behind the analysis, and how to propose a
change.

This is not medical advice. The individual studies are published research, but this combination of methods has not
been clinically validated. See [DISCLAIMER.md](../DISCLAIMER.md).

## The evidence registry

All of it lives in one file, `src/fitbit_air/analysis/evidence.toml`. It has four kinds of entry:

- **`[[source]]`:** one study, guideline or design choice, with an `id`, a `citation`, a `doi` where one exists,
  `used_for` (the scores it backs) and `effect` (what the analysis takes from it). Our own choices have
  `design_choice = true`, and their ids start with `design_` (or are `safety_thresholds`).
- **`[params]`:** every number the analysis uses, by name: windows, cut-offs, weights and limits. The code reads
  numbers only from here.
- **`[tables]`:** anchor points for curves taken from a study, such as steps against relative risk. Each table has a
  `kind`: `step`, `linear`, or `loglinear` (for hazard ratios).
- **`[bounds]`:** explicit override ranges for params where the default rule doesn't fit.

Scores and findings cite source ids. You can read them in three places:
- the `evidence_ids` column of the `scores_daily` and `insights` tables;
- the coach's `get_evidence` tool, which also explains your own overrides (`override:<param>`) and plugin findings
  (`plugin:<name>`);
- this page, for the full list.

## Principles

- **Your own baselines.** Recovery and strain compare you with yourself: a rolling median and spread over the last
  60 days. Recovery needs 7 valid days and strain signals 14. Until then, the report says "building baseline".
- **Confidence gating.** Each baseline has a confidence that grows with the amount of data. Recovery findings need
  a minimum confidence, and the coach describes low-confidence or provisional results as such, never as fact.
- **Absolute safety rails.** A few checks don't depend on your baseline: for example a high resting heart rate or a
  low overnight SpO2 on several nights. They are shown before everything else.
- **Safety rails only tighten.** Every param that the safety rails or strain signals read has a fixed direction
  (`SAFETY_DIRECTIONS` in `analysis/evidence.py`). An override may only make it stricter, and some are locked. The
  training limits work the same way: weekly long-run growth of about 10% at most, regular cutback weeks, and weight
  loss of about 1% of body weight a week at most.
- **Overrides stay within bounds.** By default a param may move between a quarter and four times its default. The
  `[bounds]` section narrows that where needed. Overrides are always shown, never hidden.
- **Design choices are labelled.** Where no study gives a number (for example how the sleep score's parts are
  weighted), the choice is a `design_` source, and its numbers live in `[params]`.

## Proposing a change

1. **Open an issue** with the citation: the paper, its DOI, and the number or method you want to change.
2. **Add the source** to `evidence.toml` as a `[[source]]` entry, with its DOI where one exists, `used_for` and
   `effect`.
3. **Add or change the param** in `[params]`, and its bounds in `[bounds]` if the default rule doesn't fit. If the
   safety rails or strain signals read it, add it to `SAFETY_DIRECTIONS`; a test checks this.
4. **Add a test** that shows the new behaviour on synthetic data.
5. **Cite the source id** from the score or finding that uses it.

A change may make a safety rail or training limit stricter, never looser. See [CONTRIBUTING.md](../CONTRIBUTING.md)
for the rest of the workflow.

## Sources by domain

This list is generated from `evidence.toml`. The domain is the first score each source backs; many sources back
more than one. The full citation and effect of each source are in the file.

| Domain | Source id | Basis |
|---|---|---|
| Baselines | `design_baseline_minimums` | design choice |
| Recovery | `plews2013_hrv` | doi `10.1007/s40279-013-0071-8` |
| Recovery | `buchheit2014_monitoring` | doi `10.3389/fphys.2014.00073` |
| Recovery | `fullagar2015_sleep_athletes` | doi `10.1007/s40279-014-0260-0` |
| Recovery | `design_recovery_weights` | design choice |
| Sleep | `buysse2014_ru_sated` | doi `10.5665/sleep.3298` |
| Sleep | `watson2015_sleep_duration` | doi `10.5665/sleep.4716` |
| Sleep | `phillips2017_sri` | doi `10.1038/s41598-017-03171-4` |
| Sleep | `windred2024_sri_mortality` | doi `10.1093/sleep/zsad253` |
| Sleep | `design_sleep_weights` | design choice |
| Sleep | `design_sri_mapping` | design choice |
| Strain signals | `mishra2020_presymptomatic` | doi `10.1038/s41551-020-00640-6` |
| Strain signals | `quer2021_wearables_covid` | doi `10.1038/s41591-020-1123-x` |
| Strain signals | `page1954_cusum` | doi `10.1093/biomet/41.1-2.100` |
| Stress index | `design_stress_index` | design choice |
| Safety rails | `safety_thresholds` | design choice |
| Activity | `ding2025_steps` | doi `10.1016/S2468-2667(25)00164-1` |
| Activity | `who2020_pa` | doi `10.1136/bjsports-2020-102955` |
| Activity | `ekelund2019_sedentary` | doi `10.1136/bmj.l4570` |
| Activity | `tudorlocke2019_cadence` | doi `10.1186/s12966-019-0769-6` |
| Weight | `shcherbina2017_ee_accuracy` | doi `10.3390/jpm7020003` |
| Weight | `donnelly2009_acsm_weight` | doi `10.1249/MSS.0b013e3181949333` |
| Running | `karvonen1957` | no DOI |
| Running | `tanaka2001_hrmax` | doi `10.1016/S0735-1097(00)01054-8` |
| Running | `frandsen2025_spikes` | no DOI |
| Running | `seiler2010_intensity` | doi `10.1123/ijspp.5.3.276` |
| Running | `friel_decoupling` | no DOI |
| Running | `riegel1981` | no DOI |
| Running | `uth2004_vo2max` | doi `10.1007/s00421-003-0988-y` |
| Running | `design_weekly_progression` | design choice |
| Running | `design_forecast_cutback` | design choice |
| Body age | `kodama2009_crf` | doi `10.1001/jama.2009.681` |
| Body age | `kaminsky2022_friend` | doi `10.1016/j.mayocp.2021.08.020` |
| Body age | `zhang2016_rhr` | doi `10.1503/cmaj.150535` |
| Body age | `arem2015_pa` | doi `10.1001/jamainternmed.2015.0533` |
| Body age | `momma2022_strength` | doi `10.1136/bjsports-2021-105061` |
| Body age | `cappuccio2010_sleep` | doi `10.1093/sleep/33.5.585` |
| Body age | `gbmc2016_bmi` | doi `10.1016/S0140-6736(16)30175-1` |
| Body age | `pang2021_hr_to_age` | doi `10.1093/aje/kwab178` |
| Body age | `whoop_age_method` | no DOI |
| Body age | `design_body_age_overlap` | design choice |
| Body age | `design_reference_person` | design choice |
