# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting: open the Security tab of
https://github.com/v4ikartana/fitbit-air-coach and choose "Report a vulnerability".

**Don't open a public issue for a vulnerability**, and don't include real health data, tokens or credentials in a
report. A minimal reproduction with synthetic data is best.

You'll get an acknowledgement when the maintainer has read the report, and a note when a fix is released.

## Supported versions

Only the latest commit on the default branch receives fixes.

## Scope

In scope:
- **OAuth token handling:** how tokens are obtained, stored in the macOS Keychain, refreshed and used.
- **The read-only SQL guard:** the coach's `query_sql` tool must run only a single read-only `SELECT`.
- **Plugin loading:** that plugins stay off unless enabled and load only from `<home>/plugins`. Plugins are
  trusted code you install yourself (see [docs/plugins.md](docs/plugins.md)); what a plugin does once loaded is out of
  scope.
- **Local file permissions:** the home folder (`~/.fitbit-air`: data, configuration, logs) and the database stay
  private to your user.

Out of scope: the Google Health API, Fitbit devices and apps, Claude and other third-party services.

## Bounty

There is no bug bounty. Credit is given in the release notes if you'd like it.
