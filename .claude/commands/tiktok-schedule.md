---
description: Schedule a finished series onto TikTok on a fixed daily rhythm — plan every episode's slot, then top up as TikTok's scheduling window rolls forward.
argument-hint: [series + cadence, e.g. "lc-series eps 17-60, 2/day at 11:30 and 19:30 from 2026-09-03, #coding #livecoding"]
---

Read `skills/core/tiktok-scheduled-publishing.md` first, then run that protocol for the request below.

The tool is `tiktok_publish` (`tools/publishers/tiktok_publish.py`, capability `publish`, provider `tiktok`) with four actions: `import_cookies`, `plan`, `status`, `run`.

Non-negotiables from the skill:

- **Measure TikTok's scheduling window; never quote it from memory.** It was 30 days on 2026-09-01, while most guides still say 10. The calendar's `valid` class on each `span.day` is the authority. Build the plan file, push what fits, and report `next_top_up` — do not tell the user the whole season is scheduled when it isn't.
- **Confirm the first unposted episode** before planning. Anything already posted by hand is invisible to the tool and will be duplicated.
- **Slot times are in the account's timezone**, not the machine's.
- **Dry-run one slot with a visible browser and read the date/time off the screenshot** before the batch. A `dry_run` slot is still owed, so it never consumes an episode.
- Budget ~90-110s per episode and run long batches in the background, not against a foreground timeout. Runs are **headless by default** — do not open a visible browser for a batch.
- **Finish every batch with `action: "verify"`** — it reconciles the plan against the account and resets anything TikTok does not actually hold. Report the reconciled count, never the runner's own. A submit can pass every visible step and still land nothing (the "Continue to post?" dialog).
- Approval gate after `plan`, before any real `run` — everything past it creates public posts.
- Cookies are credentials: import from the user's exported file, never echo values, never commit them.

Close by writing the returned `publish_log` into the project's artifacts and appending a `decision_log` entry (`category: "distribution_schedule"`).

Request: $ARGUMENTS
