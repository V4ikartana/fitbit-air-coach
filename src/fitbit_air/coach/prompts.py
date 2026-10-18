"""Prompt texts the user can pick in Claude."""
from __future__ import annotations

from datetime import date, timedelta


def morning_checkin(today: date | None = None) -> str:
    day = today or date.today()
    yesterday = day - timedelta(days=1)
    return f"""Morning check-in for {day}. Call data_status, get_overview(date="{day}"), get_coach_notes(due_by="{day}")
and get_journal(start="{yesterday}", end="{yesterday}"). Then write a brief of at most 150 words:
1. Any safety insight first, with the doctor advice from your instructions.
2. Recovery and sleep, one line each, with their confidence (say if baselines are still building).
3. ONE suggestion for today that fits the race plan (if any) and the hard limits (easy run, rest, brisk walk...).
4. Coach notes due today or overdue.
If data is stale, say so first and offer refresh_data. You may end with one question to log in the journal."""


PLAN_NEXT_RUN = """Plan my next run. Call get_running (last 30 days), get_overview (today) and get_coach_notes.
Propose the next run's distance, effort (easy unless the plan says otherwise) and run-walk structure, staying within
about +10% a week, the spike flags and the race plan's cutback weeks (if there is a plan). If recovery is low or
there is a safety insight, propose rest or a walk instead. Offer to save it with
save_coach_note(kind="plan", review_on=<the run date>)."""


def weekly_review(week_ending: str | None = None) -> str:
    end = date.fromisoformat(week_ending) if week_ending else date.today()
    start, prev_start, next_review = end - timedelta(days=6), end - timedelta(days=34), end + timedelta(days=7)
    return f"""Weekly review for {start}..{end}. Call get_trends for rhr, steps_7d, mvpa_week, sedentary_h,
sleep_debt_h, weight_trend_kg, score:recovery and score:sleep from {prev_start} to {end}, plus get_running and
get_coach_notes (status active). Compare the last 7 days with the 28 before. Report what improved, what slipped
and the race plan status if a race is configured, in your persona's style. Agree at most three targets for next
week with the user, and save each with save_coach_note(kind="target", review_on="{next_review}"). Mark last
week's targets done or dropped."""


def explain_score(score: str, date: str | None = None) -> str:
    when = date or "today"
    return f"""Explain my {score} score for {when}. Fetch it (get_overview, or get_body_age for body_age, or
get_running for race, only if a race is configured), then get_evidence for its evidence_ids. In plain words: what
drove it, how confident it is, and which single lever would move it most."""
