# Contributing

Keep all tests offline and deterministic. Never use production Garmin or Telegram credentials in issues, pull requests, logs, fixtures, screenshots, or CI.

1. Create a Python 3.13 virtual environment and install `requirements.lock`.
2. Make focused changes to the single authoritative `garmin_cal_bot.py` and add meaningful fake-service tests.
3. Run `python -m unittest discover -s tests -v`, `python -m compileall -q garmin_cal_bot.py tests`, `python -m tabnanny garmin_cal_bot.py tests`, and `python -m pip check`.
4. Review any changed files for secrets and private health data before sharing.

Describe behavior changes, failure handling, and any Garmin/Telegram API assumptions in a contribution. Do not run live integration tests against someone else's account or bot.
