# Disclaimer

The repository is fitbit-air-coach; the package and command are `fitbit-air`.

**This is not medical advice.** fitbit-air is not a medical device and does not diagnose, treat or prevent any
condition.

- The scores, signals and estimates (sleep, recovery, strain, body age, training load and the rest) are statistical
  estimates computed from a consumer wearable. They can be wrong, and they can miss things.
- The science behind each calculation is cited in `src/fitbit_air/analysis/evidence.toml`. The individual studies
  are published research, but this particular combination of methods has not been clinically validated.
- If you have symptoms, or a reading worries you, talk to a clinician. Never delay or ignore medical advice because of
  something this software or the coach said.
- The Claude coach is guided by these same limits. It is a language model working from your data, not a doctor.

## No warranty

- This software is provided "AS IS", without warranties or conditions of any kind, express or implied. See sections
  7 and 8 of the [LICENSE](LICENSE) (Disclaimer of Warranty and Limitation of Liability).
- The data may be incomplete or wrong: a sync can miss data, the API can change, and the wearable's readings have
  their own errors.
- You are responsible for your credentials and your data: your Google OAuth client, your login, your home folder
  and anything you share with Claude or anyone else.

## Not affiliated

This is an unofficial, independent project. It is not affiliated with, endorsed by or sponsored by Fitbit, Google or
Anthropic. It reads your own data through the Google Health API v4 using your own credentials.

"Fitbit" and "Google" are trademarks of Google LLC. "Claude" is a trademark of Anthropic. Other names are trademarks
of their respective owners.
