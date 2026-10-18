"""Profiles for tests that need a race, body age (male reference data) or goals."""
from fitbit_air.analysis.profile import Profile

RUNNER = Profile(sex="male", race_date="2027-02-07", race_distance_km=10.0,
                 goals=["weight", "cardio", "sleep", "recovery", "running_10k"])
