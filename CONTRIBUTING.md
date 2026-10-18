# Contributing

Thanks for your interest. Issues and pull requests are welcome. Please read [DISCLAIMER.md](DISCLAIMER.md) first: this
project handles health data and gives no medical advice.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12. The project is tested on macOS.

```bash
uv sync
uv run pytest
```

The tests use only synthetic data. They need no Fitbit account, no Google credentials and no network.

No band? `uv run python scripts/make_demo_home.py ~/.fitbit-air-demo` builds a demo home with made-up data,
then `FITBIT_AIR_HOME=~/.fitbit-air-demo uv run fitbit-air report`.

## Ground rules

- **Test first.** Write a failing test, make it pass, then tidy up. Every change comes with tests.
- **Cite the science.** Any change to a score, threshold or insight cites its source in
  `src/fitbit_air/analysis/evidence.toml`. Code reads numeric parameters from that file; don't hard-code them. Mark
  your own choices with `design_choice = true`.
- **Safety rails only get stricter.** The limits on running load, weight loss, illness and the safety insights may be
  tightened, never loosened.
- **No personal data.** Tests use the synthetic payloads in `tests/fixtures/api/` and the synthetic seeds in `tests/`.
  Never commit a real export, a database, a token, a home path or anything that identifies a person.
- **Scan before you open a PR:**

  ```bash
  uv run python scripts/check_public.py .
  ```

  It must print `clean`.

## How to add

- **A configuration template:** add a folder under `src/fitbit_air/templates/` with a `config/` directory, modelled on
  `minimal`, and add its name to `TEMPLATES` in `src/fitbit_air/home.py`. `tests/test_home_init.py` then checks that
  it initialises and passes `fitbit-air config check`.
- **A plugin example:** follow [docs/plugins.md](docs/plugins.md). Examples must run on the synthetic fixtures and
  stay read-only.
- **An evidence source:** add a `[[source]]` entry to `evidence.toml` with `id`, `citation`, `doi` (where one exists),
  `used_for` and `effect`, and reference its id from the score or insight that uses it.

## Style

- Lines are 120 characters or fewer.
- Type hints on public functions.
- Small, focused modules; standard library first. A new dependency needs a reason in the PR.

## Maintainer tools

`scripts/export_public.py`, `scripts/build_private_scan_list.py` and `scripts/make_fixtures.py` belong to the
maintainer's private-to-public export: they build the published copy of this repo and its synthetic fixtures from a
private working copy. They are not needed to contribute; `scripts/check_public.py` is the only one to run before a PR.

## Releases

Releases are made by the maintainers only.
