args: week_ending
Weekly review for the 7 days ending {week_ending}. Call get_trends for rhr, steps_7d, mvpa_week, sedentary_h,
sleep_debt_h, weight_trend_kg, score:recovery and score:sleep for the 35 days up to {week_ending}, plus get_running
and get_coach_notes (status active). Compare the last 7 days with the 28 before. Report what improved, what slipped
and the race plan status if a race is configured, in your persona's style. Agree at most three targets for next
week with the user, and save each with save_coach_note(kind="target", review_on="{next_week}"). Mark last
week's targets done or dropped.
