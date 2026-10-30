# TikTok Scheduled Publishing — Core Skill

## When to Use

When a finished series (or any batch of renders) needs to go out on TikTok on a
fixed rhythm — "two a day, 11:30 and 19:30, starting Thursday" — rather than one
video at a time by hand. Also when a single render needs a scheduled slot instead
of an immediate post.

Not for: packaging a render for hand-off with no upload (`export_bundle`), or
platforms other than TikTok.

## The Constraint That Shapes Everything

**TikTok Studio refuses a schedule beyond a fixed window — and you must measure
that window, not look it up.**

Measured off the live calendar on 2026-09-01: every day from today through **+30
days** was selectable, +31 onward was not. Most published guides — and most
model priors — still say *10 days*. They are stale. Getting this wrong in either
direction is expensive: assume 10 and you invent a top-up schedule nobody needs;
assume 30 when the account gets 10 and you report 44 posts scheduled when 18
landed.

The authority is the calendar itself. Each day cell is
`span.day`, and it carries the extra class **`valid`** only when TikTok will
accept it. `_fill_date` checks for `valid` and raises a named blocker rather than
clicking a dead cell, so a window that shrinks degrades into a clear per-slot
error instead of a silent miss.

When a run is longer than the window, the shape is **one plan plus a top-up**:

1. **A plan** — every episode gets a fixed slot, written to disk once.
2. **A top-up** — a re-runnable pass that pushes slots as they enter the window.

`run` is a no-op when nothing is due, which is what makes it safe on a timer.

Other hard requirements, worth checking before any of this:

| Requirement | Why it bites |
|---|---|
| Creator or Business account | Personal accounts do not render the Schedule control at all |
| Desktop web (TikTok Studio) | The mobile app cannot schedule |
| Account timezone | TikTok schedules in **the account's** timezone, not the machine's |
| Minutes on a 5-minute grid | The time picker only offers `00, 05, … 55`. 11:37 cannot be selected |
| A valid cookie jar | `sessionid` is what authenticates; a stale export lands on `/login` |

## Prerequisites

| Resource | Where |
|---|---|
| Tool | `tiktok_publish` (`tools/publishers/tiktok_publish.py`), capability `publish`, provider `tiktok` |
| Runtime | `pip install playwright && playwright install chromium` |
| Auth | A cookie jar the user exports from a browser extension (Cookie-Editor / EditThisCookie) while signed in |
| Renders | One finished video per episode, e.g. `projects/<slug>/renders/final.mp4` |
| Artifact | `publish_log` — `schemas/artifacts/publish_log.schema.json` (`status: "scheduled"`, `scheduled_for`) |

## Process

### Step 1: Collect the inputs

Five things, and none of them should be guessed:

| Input | Notes |
|---|---|
| **Cookies** | Ask for the exported file path. Never ask the user to paste cookie *values* into chat. |
| **Series code** | The `[LC]` in `[LC] Tập 17: …`. If episodes are already posted by hand, match the code they used — a prefix that changes mid-series breaks the feed's navigability. Ask; do not invent. |
| **Hashtags** | A flat list. The tool prefixes `#` itself, so `coding` and `#coding` both work. |
| **Times of day** | In the **account's** timezone, on the 5-minute grid. `["11:30", "19:30"]` fills each day in order. |
| **Metadata** | Episode number, title, and the path to the render, in posting order. |

Then confirm the boundary explicitly: **which episode is the first unposted
one.** Re-scheduling an episode the user already posted by hand produces a
duplicate, and TikTok gives you no warning.

### Step 2: Import the cookies

```python
tiktok_publish.execute({
    "action": "import_cookies",
    "cookies_path": "~/Downloads/www.tiktok.com_22-08-2026.json",
    "account": "default",
})
```

The jar is copied to `~/.openmontage/cookies/tiktok-<account>.json` at 0600 —
outside the repo, never committed. The result reports counts, domains and expiry
dates; it never returns a value, and neither should you. Check `has_sessionid`
is true. A handful of expired short-lived cookies (`tt_csrf_token`) is normal; an
expired `sessionid` is not.

### Step 3: Build the plan

```python
tiktok_publish.execute({
    "action": "plan",
    "plan_path": "projects/_lc-series/tiktok_schedule.json",
    "series_code": "LC", "series_id": "lc-series",
    "hashtags": ["coding", "livecoding", "live_coding"],
    "start_date": "2026-09-03",
    "times": ["11:30", "19:30"],
    "visibility": "public",
    "episodes": [{"ep": 17, "title": "…", "video": "projects/lc-ep17-…/renders/final.mp4"}, …],
})
```

The caption is built as:

```
[LC] Tập 17: Tàu điện có giá vé khác nhau
#coding #livecoding #live_coding
```

Title line first because TikTok truncates in-feed after roughly one line; the
series code and episode number are what let a viewer navigate 60 episodes.

`plan` is **idempotent and safe to re-run**: any slot already in state
`scheduled` is carried across untouched, so fixing a typo in a later episode's
title never disturbs what is already live on TikTok's scheduler.

Read `missing_video` in the result before going further. Then show the user
`sample_caption`, `first_slot` and `last_slot` and **get approval** — this is the
gate. Everything after it creates real public posts.

### Step 4: Verify one slot before the batch

Push exactly one episode with a visible browser and `dry_run`:

```python
tiktok_publish.execute({
    "action": "run", "plan_path": "…", "only": [17], "dry_run": True, "headless": False,
})
```

`dry_run` fills the entire form — file, caption, visibility, date, time — and
screenshots it **without pressing Post**. Open that screenshot and read the date
and the time back off the page.

Do this every time. It is how the 10-vs-30-day error above was caught, and how
the tour overlay below was found. `dry_run` leaves the slot in state `dry_run`,
which a later real run still treats as owed — a rehearsal never consumes a slot.

### Step 5: Run the batch

```python
tiktok_publish.execute({"action": "run", "plan_path": "…"})
```

Runs **headless by default** — a long batch must not hold the operator's screen
hostage for an hour. Pass `headless: False` only to watch a first run or debug.

Only slots that are still owed, still in the future, and inside the window are
pushed. Budget **~90-110s per episode** — most of it is TikTok transcoding a 4-6
minute vertical file before the Post button enables. Forty-four episodes is over
an hour, so run it in the background rather than against a foreground timeout.

The plan is rewritten after every slot, so an interruption costs one episode.

### Step 5b: Reconcile — this step is not optional

```python
tiktok_publish.execute({"action": "verify", "plan_path": "…"})
```

`verify` reads the account's own item-list API and confirms a slot only when a
post for **this series** exists **at the planned minute**. **Report the
reconciled numbers, never the runner's own** — `confirmed_at_planned_time`,
`wrong_time` and `reset_to_pending` are the truth; `pushed` is only what the
tool believed. Re-run after a reset; `run` picks the resets up.

Three traps are baked into it, each of which produced a wrong answer first:

- **Do not scrape the Posts table.** It is virtualised — roughly nine rows exist
  in the DOM at a time, so `innerText` returns a moving slice no matter how you
  scroll. That read reported 9 of 44 present and queued the other 35 for
  re-posting. Read the `creator/manage/item_list` JSON the page already fetches,
  and page it until `has_more: false`.
- **Anchor the caption match on the series code.** A bare `Tập\s*(\d+):` also
  matched an unrelated `#system_design` series on the same account, silently
  confirming episodes that were never posted.
- **Never reconcile on a partial read.** An under-count marks live posts as
  pending and duplicates them. If the listing does not complete, raise.

### Step 6: Arrange the top-up (only if the run exceeds the window)

`next_top_up` is the date the next pending slot enters the window; it is `null`
when everything already fits. Two ways to cover it, and the choice is the user's:

- **Automatic** — a `launchd` (macOS) or `cron` entry running `run` daily. Ask
  before installing one: it is a persistent, outward-facing side effect.
- **Manual** — the user re-runs the command every few days. `status` always says
  what is owed and by when.

### Step 7: Record the decision and the artifact

Write the returned `publish_log` into the project's artifacts and append a
`decision_log` entry (`category: "distribution_schedule"`) recording the cadence,
the start date, the measured window, and the top-up arrangement. If the cadence
later changes, **append a new entry with the same `subject`** — see the Re-log
Changed Decisions rule in `AGENT_GUIDE.md`.

## What the Live DOM Actually Looks Like

Read off TikTok Studio on 2026-09-01. Every one of these replaced a plausible
guess that silently did nothing — if the flow breaks, re-probe before rewriting.

| Thing | Reality |
|---|---|
| Product tour | `react-joyride` drops a full-page overlay with a **"Got it"** button that swallows *every* click, and it reappears mid-flow. Dismiss it before each step; removing `#react-joyride-portal` is the reliable path |
| Now / Schedule toggle | A real radio: `input[name="postSchedule"][value="schedule"]` — click it, don't hunt for the word "Schedule" |
| Date & time fields | **Readonly** `input.TUXTextInputCore-input`. `fill()` does nothing at all; only the popups work |
| Calendar | `.calendar-wrapper`, header `.month-title` + `.year-title`, `.month-header-wrapper .arrow` (0 = prev, 1 = next), cells `span.day`, selectable only with `.valid` |
| Time picker | `span.tiktok-timepicker-option-text`, `…-left` = hours `00-23`, `…-right` = minutes in 5-minute steps |
| Caption box | A `div[contenteditable]` **pre-filled with the filename** ("final"). Clear it first or the caption is appended to junk |
| Hashtag entry | Typing `#` opens an autocomplete that eats the next keystroke. Escape after each tag |
| **The confirm dialog** | If TikTok's content check has not finished, pressing Post opens **"Continue to post?"**. Its confirm button says **"Post now"** — that means *proceed*, and it still honours the Schedule setting. Miss the dialog and the submission is silently abandoned when the page closes |

**The read-back is the safety net.** After setting the schedule, the tool reads
the two inputs and compares them to the target, failing loudly on mismatch. A
date picker can accept a click on the wrong cell and look perfectly normal;
nothing but reading the value back catches that.

**And the account is the only proof of a post.** A submit can succeed at every
observable step and still leave nothing behind — that is exactly what the
confirm dialog above caused, for ten consecutive episodes, all of them reported
as `scheduled`. Two rules follow:

- `_submit` waits for the schedule form to **unmount**, which is what TikTok
  accepting the post actually looks like. A `wait_for_timeout` proves nothing.
- **Always finish a batch with `action: "verify"`.** It reads the Posts page and
  resets any slot the account does not hold back to `pending`. Never report a
  count from the runner's own state; report the reconciled one.

## Self-Evaluation

| Criterion | 1 | 3 | 5 |
|---|---|---|---|
| Window handling | Assumed a number from memory | Quoted the docs | Measured it off the calendar and said when it was measured |
| Boundary correctness | Re-scheduled already-posted episodes | Asked, but did not verify | Confirmed the first unposted episode |
| Caption fidelity | Prefix or numbering wrong | Right format, invented code | Format and series code both confirmed with the user |
| Verification | Batched straight away | Watched the first one go | Dry-ran and read the date/time off the screenshot |
| Timezone | Assumed the machine's | Mentioned it | Confirmed the account's timezone before planning |
| Credential handling | Cookie values in chat or repo | Stored loosely | 0600 outside the repo, only counts/expiries reported |

Anything scoring 1 or 2 must be fixed before the batch, not after.

## Common Pitfalls

- **Trusting a remembered limit.** "TikTok allows 10 days" is the single most
  confidently wrong fact in this workflow. Measure.
- **Trusting "it clicked".** Playwright reports success for a click that landed
  on a dead cell, and for a Post that a modal then swallowed. Read the value
  back, and reconcile against the account before reporting anything.
- **Reporting the runner's state as the outcome.** The plan file records what
  the tool *believed*. Only `verify` records what TikTok *has*.
- **Fighting the overlay instead of removing it.** Every "element intercepts
  pointer events" failure here is the joyride tour.
- **`fill()` on the date field.** It is readonly; the call succeeds and changes
  nothing, which is the worst possible failure mode.
- **The machine's timezone.** 11:30 on your laptop is not 11:30 on the account.
- **Re-posting by hand and by tool.** Anything already scheduled manually must be
  excluded from `episodes` — the tool cannot see it.
- **A stale jar mid-run.** Cookies expire during a long campaign. A run that
  lands on `/login` fails loudly with `[tiktok:session]` — re-export, re-import,
  re-run. Already-scheduled slots are untouched.
- **Reporting a dry run as scheduled.** `dry_run` leaves state `dry_run` and a
  `publish_log` entry of `draft`. Do not describe it as posted.
