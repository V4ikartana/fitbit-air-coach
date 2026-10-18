## What changed and why

## Tests

- [ ] New or changed behaviour has tests, written first
- [ ] `uv run pytest` passes

## Science and safety

- [ ] This touches a score, threshold or insight, and the source is cited in `evidence.toml`
- [ ] This touches a safety rail (it may only make it stricter)
- [ ] Neither of the above

## Privacy

- [ ] No personal data: tests use `tests/fixtures/api/` and synthetic seeds only
- [ ] `uv run python scripts/check_public.py .` prints `clean`
