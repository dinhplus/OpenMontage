"""TikTok scheduled publishing — the first *networked* publisher.

`export_bundle` packages a render for hand-off; this tool takes it the last mile
and puts a whole series on TikTok's own scheduler, one fixed slot per episode.

Why a plan file instead of a loop over episodes
-----------------------------------------------
**TikTok Studio refuses a schedule beyond a fixed window.** Measured against the
live UI on 2026-09-01 the window is **30 days**, not the 10 that most published
guides still quote — so the number below is a default, never a promise. The
authority is the calendar itself: a day cell carries the class `valid` only when
TikTok will accept it, and `_fill_date` refuses to guess when it does not.

A run longer than the window is therefore one plan plus a top-up that repeats
until the plan is exhausted. The plan lives on disk (`tiktok_schedule.json`),
each slot carries its own state, and the file is rewritten after *every* slot,
so a crash, a closed laptop, or an expired cookie costs one episode rather than
the run.

That makes the tool safely re-runnable, which is the whole design: `run` pushes
only the slots that are (a) still `pending` and (b) inside the horizon. Running
it twice in a day is a no-op, and it is meant to be run from cron.

Authentication
--------------
Cookies exported from a browser extension (Cookie-Editor and friends), converted
to Playwright `storage_state`. A TikTok `sessionid` is enough to act as the
account, so the jar is copied to `~/.openmontage/cookies/` at 0600 — outside the
repo, never in git, and never echoed back by this tool, which reports counts and
expiry dates only.

The expiry normalisation below is carried over from a sibling project that hit
the bug in production: Chrome hands back **microseconds since 1601**, and feeding
that to `datetime.fromtimestamp` raises "year 426460186 is out of range".

Scope
-----
This automates a flow the account owner could perform by hand, on their own
account, at human pace. There is no proxy rotation, no fingerprint shaping, and
no multi-account orchestration here, and none should be added.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ResumeSupport,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
PROFILE_URL = "https://www.tiktok.com/tiktokstudio/content"

# How far ahead TikTok Studio will accept a schedule. Measured off the live
# calendar on 2026-09-01: every day from today through +30 was selectable and
# +31 onward was not. Published guides still say 10; they are stale. This is
# only used to decide which slots are worth *attempting* — the calendar's own
# `valid` class is what actually gates a date, so a shrunken window degrades
# into a clear per-slot error rather than a wrong post.
SCHEDULE_HORIZON_DAYS = 30

# TikTok's caption box counts characters, not bytes.
CAPTION_LIMIT = 2200

COOKIE_HOME = Path.home() / ".openmontage" / "cookies"

STEP_TIMEOUT_MS = 120_000
PROCESSING_TIMEOUT_MS = 1_800_000

# The Studio UI ships in the account's own language. Every label this tool
# clicks is listed in both, longest-lived phrasing first. Add a locale here
# rather than reaching for a brittle CSS path.
LABELS: dict[str, list[str]] = {
    "schedule": ["Schedule", "Lên lịch", "Hẹn giờ"],
    "post_now": ["Post now", "Đăng ngay"],
    "post": ["Post", "Đăng"],
    "who_can_watch": ["Who can watch this video", "Ai có thể xem video này"],
    "everyone": ["Everyone", "Mọi người", "Công khai"],
    "uploading": [
        "Your video is being uploaded",
        "Video của bạn đang được tải lên",
        "scheduled",
        "đã lên lịch",
    ],
}

_VISIBILITY_LABELS = {
    "public": ["Everyone", "Mọi người", "Công khai"],
    "unlisted": ["Friends", "Bạn bè"],
    "private": ["Only you", "Chỉ mình tôi"],
}


class TikTokPublishError(RuntimeError):
    """A publishing step failed, with a screenshot path when one was taken."""

    def __init__(self, step: str, message: str, screenshot: str = "") -> None:
        detail = f"[tiktok:{step}] {message}"
        if screenshot:
            detail += f"\nScreenshot: {screenshot}"
        super().__init__(detail)
        self.step = step
        self.screenshot = screenshot


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #

_WEBKIT_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01
_PLAUSIBLE = (946_684_800, 7_258_118_400)  # 2000-01-01 .. 2200-01-01


def normalise_expiry(value: float) -> int:
    """Any cookie store's expiry as Unix seconds, or 0 when it is not a date.

    Tried in order of how common they are, first plausible reading wins. Order
    matters: 13457721483000000 read as microseconds-since-1970 gives the year
    2396 — wrong, but not obviously so — while as microseconds-since-1601 it
    gives 2027, which is what the cookie actually says.
    """
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    low, high = _PLAUSIBLE
    for candidate in (
        value,                                      # seconds since 1970
        value // 1_000,                             # milliseconds since 1970
        value // 1_000_000 - _WEBKIT_EPOCH_OFFSET,  # microseconds since 1601
        value // 1_000_000,                         # microseconds since 1970
        value - _WEBKIT_EPOCH_OFFSET,               # seconds since 1601
    ):
        if low <= candidate <= high:
            return candidate
    return 0


def parse_cookie_export(text: str) -> list[dict[str, Any]]:
    """Normalise an exported jar to Playwright cookie dicts.

    Accepts both shapes real extensions produce: the JSON array (or
    ``{"cookies": [...]}``) that Cookie-Editor writes, and a Netscape
    ``cookies.txt``.
    """
    stripped = text.lstrip("﻿").strip()
    rows = (
        _from_json(stripped)
        if stripped.startswith(("{", "["))
        else _from_netscape(text)
    )
    if not rows:
        raise ValueError(
            "no cookies found. Export the whole cookie list for tiktok.com — "
            "either the JSON an extension writes or a Netscape cookies.txt."
        )
    return rows


def _from_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc

    entries = data.get("cookies", data.get("Cookies")) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(
            "expected a JSON array of cookies (or an object with a 'cookies' array)"
        )

    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, domain = entry.get("name"), entry.get("domain")
        if not name or not domain:
            continue
        # Playwright wants -1 for a session cookie, not 0.
        expires = -1 if entry.get("session") else normalise_expiry(entry.get("expirationDate") or 0)
        out.append(
            {
                "name": str(name),
                "value": str(entry.get("value") or ""),
                "domain": str(domain),
                "path": str(entry.get("path") or "/"),
                "expires": float(expires if expires else -1),
                "httpOnly": bool(entry.get("httpOnly")),
                "secure": bool(entry.get("secure")),
                "sameSite": _same_site(entry.get("sameSite")),
            }
        )
    return out


def _from_netscape(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.rstrip("\r").strip()
        if not line:
            continue
        if line.startswith("#"):
            # `#HttpOnly_domain` is a real entry, not a comment.
            if not line.startswith("#HttpOnly_"):
                continue
            line = line[len("#HttpOnly_"):]
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _include_sub, path, secure, expires, name, value = fields
        stamp = normalise_expiry(expires)
        out.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "expires": float(stamp if stamp else -1),
                "httpOnly": False,
                "secure": secure.upper() == "TRUE",
                "sameSite": "Lax",
            }
        )
    return out


def _same_site(value: Any) -> str:
    """Playwright accepts exactly Strict / Lax / None."""
    mapping = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None"}
    return mapping.get(str(value or "").lower(), "Lax")


def summarise_cookies(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, domains and expiry dates — never a value. Safe to print."""
    now = datetime.now().timestamp()
    dated = [c["expires"] for c in cookies if c["expires"] and c["expires"] > 0]
    session_bearing = sorted(
        {
            c["name"]
            for c in cookies
            if re.search(r"sessionid|sid_tt|sid_guard|passport|auth|token", c["name"], re.I)
        }
    )
    return {
        "count": len(cookies),
        "domains": sorted({c["domain"].lstrip(".") for c in cookies}),
        "session_cookies": sum(1 for c in cookies if c["expires"] in (-1, 0)),
        "expired": sum(1 for e in dated if e < now),
        "earliest_expiry": (
            datetime.fromtimestamp(min(dated)).isoformat(timespec="seconds") if dated else None
        ),
        "session_bearing_names": session_bearing,
        "has_sessionid": any(c["name"] == "sessionid" for c in cookies),
    }


def cookie_path(account: str) -> Path:
    return COOKIE_HOME / f"tiktok-{_slug(account)}.json"


def load_storage_state(account: str) -> dict[str, Any]:
    path = cookie_path(account)
    if not path.is_file():
        raise FileNotFoundError(
            f"no stored TikTok cookies for account {account!r}. Import them first:\n"
            f'  action="import_cookies", cookies_path="<exported file>", account="{account}"'
        )
    return {"cookies": json.loads(path.read_text(encoding="utf-8")), "origins": []}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "default").strip()).strip("-.")
    return cleaned.lower() or "default"


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def build_caption(code: str, ep: int, title: str, hashtags: Iterable[str]) -> str:
    """`[LC] Tập 17: Tàu điện có giá vé khác nhau` + a hashtag line.

    The series code and episode number are what make a 60-episode feed
    navigable, so they lead. Hashtags go on their own line because TikTok
    truncates the caption in-feed after roughly the first line.
    """
    tags = " ".join("#" + t.lstrip("#") for t in hashtags if t and t.strip())
    head = f"[{code}] Tập {ep}: {title}".strip()
    caption = f"{head}\n{tags}".strip()
    return caption[:CAPTION_LIMIT]


def slot_datetimes(start_date: str, times: list[str], count: int) -> list[datetime]:
    """`count` slots, filling each day's times in order before moving to the next.

    Fixed and fully determined by the inputs — the plan must be reproducible, or
    a re-plan after an edit would silently shuffle a half-published season.
    """
    day = datetime.strptime(start_date, "%Y-%m-%d")
    parsed = [datetime.strptime(t.strip(), "%H:%M").time() for t in times]
    if not parsed:
        raise ValueError("at least one posting time is required, e.g. ['11:30', '19:30']")
    out: list[datetime] = []
    while len(out) < count:
        for clock in parsed:
            if len(out) == count:
                break
            out.append(datetime.combine(day.date(), clock))
        day += timedelta(days=1)
    return out


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Rewrite the plan without ever leaving a truncated file behind.

    The plan is the only record of what has already been scheduled; a partial
    write during a 44-slot run would mean re-posting episodes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".plan-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Browser flow
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class _Uploader:
    """One browser context, reused across every slot in a run.

    Reused deliberately: a fresh Chromium per episode is ~4s of startup each and
    makes TikTok's rate limiter far more likely to notice.

    Every selector below was read off the live Studio DOM, not guessed:

    - the Now/Schedule toggle is a real radio, ``input[name="postSchedule"]``
    - the date and time fields are **readonly** ``input.TUXTextInputCore-input``,
      so they can only be set through their popups — `fill()` silently does
      nothing
    - the calendar is ``.calendar-wrapper`` with ``span.day``; only cells that
      also carry ``valid`` are inside TikTok's scheduling window
    - the time popup is ``.tiktok-timepicker-option-text``, split into
      ``-left`` (hours 00-23) and ``-right`` (minutes, in 5-minute steps)
    """

    def __init__(self, context: Any, evidence_dir: Path, locale_labels: dict[str, list[str]]):
        self.context = context
        self.evidence_dir = evidence_dir
        self.labels = locale_labels

    async def schedule(self, slot: dict[str, Any], visibility: str, dry_run: bool) -> dict[str, Any]:
        page = await self.context.new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)
        try:
            await self._open(page, slot)
            await self._attach(page, slot)
            await self._caption(page, slot)
            await self._visibility(page, slot, visibility)
            await self._set_schedule(page, slot)
            if dry_run:
                return {"state": "dry_run", "screenshot": await self._shot(page, slot, "dry-run")}
            await self._submit(page, slot)
            return {"state": "scheduled", "screenshot": ""}
        finally:
            await page.close()

    # -- steps ------------------------------------------------------------- #

    async def _open(self, page: Any, slot: dict[str, Any]) -> None:
        await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3_000)
        if "/login" in page.url or "/signup" in page.url:
            raise TikTokPublishError(
                "session",
                "the stored cookies are no longer valid - export a fresh jar from "
                "the browser where you are signed in and re-import it",
                await self._shot(page, slot, "session"),
            )
        await self._dismiss(page)

    async def _dismiss(self, page: Any) -> int:
        """Clear TikTok's product-tour overlay.

        `react-joyride` drops a full-page overlay that swallows every click, and
        it reappears at unpredictable points in the flow -- a run that does not
        clear it fails with "element intercepts pointer events" on whichever
        step happens to be next. Clicking its dismiss button is the polite path;
        removing the node is the one that always works.
        """
        try:
            return await page.evaluate(
                """() => {
                  let n = 0;
                  for (const b of document.querySelectorAll('button, span, div')) {
                    const t = (b.textContent || '').trim();
                    if (/^(Got it|OK|Okay|Skip|Close|Da hieu|\u0110\u00e3 hi\u1ec3u|B\u1ecf qua)$/i.test(t)
                        && b.offsetParent) { b.click(); n++; }
                  }
                  for (const s of ['#react-joyride-portal', '.react-joyride__overlay',
                                   '[data-test-id="overlay"]'])
                    document.querySelectorAll(s).forEach(e => { e.remove(); n++; });
                  return n;
                }"""
            )
        except Exception:
            return 0

    async def _attach(self, page: Any, slot: dict[str, Any]) -> None:
        video = Path(slot["video"])
        if not video.is_file():
            raise TikTokPublishError("attach", f"{video} is missing")
        try:
            target = page
            for frame in page.frames:
                if "upload" in (frame.url or ""):
                    target = frame
                    break
            await target.locator('input[type="file"]').first.set_input_files(str(video))
            # The Settings panel only mounts once the file is accepted.
            await page.locator('input[name="postSchedule"]').first.wait_for(
                state="attached", timeout=PROCESSING_TIMEOUT_MS
            )
            await page.wait_for_timeout(2_000)
            await self._dismiss(page)
        except TikTokPublishError:
            raise
        except Exception as exc:
            raise TikTokPublishError(
                "attach", str(exc), await self._shot(page, slot, "attach")
            ) from exc

    async def _caption(self, page: Any, slot: dict[str, Any]) -> None:
        """Type the caption, dismissing TikTok's hashtag autocomplete as we go.

        Two traps. The box arrives pre-filled with the *filename* ("final"), so
        it must be cleared rather than appended to. And typing `#` opens a
        suggestion popup that swallows the next keystroke, so each tag is
        followed by Escape.
        """
        try:
            await self._dismiss(page)
            editor = page.locator('div[contenteditable="true"]').first
            await editor.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
            await editor.click()
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(300)

            head, _, tag_line = slot["caption"].partition("\n")
            await page.keyboard.type(head, delay=12)
            if tag_line:
                await page.keyboard.press("Enter")
                for tag in tag_line.split():
                    await page.keyboard.type(tag, delay=25)
                    await page.wait_for_timeout(400)
                    await page.keyboard.press("Escape")
                    await page.keyboard.type(" ")
            await page.wait_for_timeout(600)

            # Read it back: a swallowed keystroke is otherwise invisible.
            written = (await editor.inner_text()).strip()
            for token in [head] + tag_line.split():
                if token.lstrip("#") not in written:
                    raise TikTokPublishError(
                        "caption",
                        f"caption came out wrong - {token!r} is missing from "
                        f"{written[:200]!r}",
                        await self._shot(page, slot, "caption"),
                    )
        except TikTokPublishError:
            raise
        except Exception as exc:
            raise TikTokPublishError(
                "caption", str(exc), await self._shot(page, slot, "caption")
            ) from exc

    async def _visibility(self, page: Any, slot: dict[str, Any], visibility: str) -> None:
        """Set who can watch. TikTok defaults to Everyone, so public is a no-op."""
        if visibility == "public":
            return
        try:
            await self._dismiss(page)
            await self._click_any(page, self.labels["who_can_watch"])
            await page.wait_for_timeout(500)
            await self._click_any(page, _VISIBILITY_LABELS[visibility])
        except Exception as exc:
            raise TikTokPublishError(
                "visibility", str(exc), await self._shot(page, slot, "visibility")
            ) from exc

    async def _set_schedule(self, page: Any, slot: dict[str, Any]) -> None:
        when = datetime.fromisoformat(slot["scheduled_for"])
        try:
            await self._dismiss(page)
            radio = page.locator('input[name="postSchedule"][value="schedule"]').first
            await radio.click(force=True)
            await page.wait_for_timeout(2_000)
            await self._fill_date(page, when)
            await self._fill_time(page, when)
            await page.wait_for_timeout(600)

            # The only check that matters: read the form back.
            got_time, got_date = await self._schedule_values(page)
            want_date, want_time = when.strftime("%Y-%m-%d"), when.strftime("%H:%M")
            if (got_date, got_time) != (want_date, want_time):
                raise TikTokPublishError(
                    "schedule",
                    f"wanted {want_date} {want_time} but the form reads "
                    f"{got_date} {got_time}",
                    await self._shot(page, slot, "schedule"),
                )
        except TikTokPublishError:
            raise
        except Exception as exc:
            raise TikTokPublishError(
                "schedule", str(exc), await self._shot(page, slot, "schedule")
            ) from exc

    @staticmethod
    async def _schedule_values(page: Any) -> tuple[str, str]:
        """(time, date) as the two readonly fields currently read."""
        return await page.evaluate(
            """() => {
              const v = [...document.querySelectorAll('input.TUXTextInputCore-input')]
                .map(i => i.value);
              return [v.find(x => /^\d{1,2}:\d{2}$/.test(x)) || '',
                      v.find(x => /^\d{4}-\d{2}-\d{2}$/.test(x)) || ''];
            }"""
        )

    async def _open_field(self, page: Any, kind: str) -> None:
        """Click the readonly date or time field to open its popup."""
        opened = await page.evaluate(
            """(kind) => {
              const re = kind === 'date' ? /^\d{4}-\d{2}-\d{2}$/ : /^\d{1,2}:\d{2}$/;
              const i = [...document.querySelectorAll('input.TUXTextInputCore-input')]
                .find(x => re.test(x.value));
              if (!i) return false;
              i.scrollIntoView({block: 'center'});
              i.click();
              return true;
            }""",
            kind,
        )
        if not opened:
            raise TikTokPublishError("schedule", f"could not find the {kind} field")
        await page.wait_for_timeout(1_500)

    async def _fill_date(self, page: Any, when: datetime) -> None:
        """Walk the calendar to the target month, then click the day.

        A cell without the `valid` class is outside TikTok's scheduling window;
        clicking it does nothing at all, so it is reported as the blocker it is
        rather than left to fail later as a mysterious wrong date.
        """
        await self._open_field(page, "date")
        for _ in range(14):
            header = await page.evaluate(
                """() => {
                  const w = document.querySelector('.calendar-wrapper');
                  if (!w) return null;
                  return [w.querySelector('.month-title')?.textContent?.trim(),
                          w.querySelector('.year-title')?.textContent?.trim()];
                }"""
            )
            if not header or not header[0]:
                raise TikTokPublishError("schedule", "the calendar popup did not open")
            shown = datetime.strptime(f"{header[0]} {header[1]}", "%B %Y")
            if (shown.year, shown.month) == (when.year, when.month):
                break
            # arrow 0 is previous month, arrow 1 is next.
            await page.locator(".month-header-wrapper .arrow").nth(
                1 if shown < when.replace(day=1, hour=0, minute=0) else 0
            ).click()
            await page.wait_for_timeout(700)
        else:
            raise TikTokPublishError(
                "schedule", f"could not reach {when:%B %Y} in the calendar"
            )

        clicked = await page.evaluate(
            """(day) => {
              const cells = [...document.querySelectorAll('.calendar-wrapper span.day')]
                .filter(c => c.textContent.trim() === String(day));
              if (!cells.length) return 'missing';
              const ok = cells.find(c => c.className.includes('valid'));
              if (!ok) return 'invalid';
              ok.click();
              return 'ok';
            }""",
            when.day,
        )
        if clicked == "invalid":
            raise TikTokPublishError(
                "schedule",
                f"{when:%Y-%m-%d} is outside TikTok's scheduling window - the "
                f"calendar shows that day as unselectable. Schedule it in a "
                f"later top-up run.",
            )
        if clicked != "ok":
            raise TikTokPublishError("schedule", f"no calendar cell for day {when.day}")
        await page.wait_for_timeout(1_000)

    async def _fill_time(self, page: Any, when: datetime) -> None:
        """Click the hour then the minute in the two scroll columns.

        Minutes come in 5-minute steps, so a slot time like 11:37 has no cell to
        click and is rejected up front rather than silently rounded.
        """
        if when.minute % 5:
            raise TikTokPublishError(
                "schedule",
                f"TikTok only offers minutes in 5-minute steps; {when:%H:%M} "
                f"cannot be selected",
            )
        await self._open_field(page, "time")
        for side, value in (("left", f"{when.hour:02d}"), ("right", f"{when.minute:02d}")):
            got = await page.evaluate(
                """([side, value]) => {
                  const cells = [...document.querySelectorAll(
                    'span.tiktok-timepicker-option-text.tiktok-timepicker-' + side)]
                    .filter(c => c.textContent.trim() === value);
                  if (!cells.length) return 'missing';
                  cells[0].scrollIntoView({block: 'center'});
                  cells[0].click();
                  return 'ok';
                }""",
                [side, value],
            )
            if got != "ok":
                raise TikTokPublishError(
                    "schedule", f"no {side} time option for {value!r}"
                )
            await page.wait_for_timeout(900)

    async def _submit(self, page: Any, slot: dict[str, Any]) -> None:
        """Press Post/Schedule, clear the confirm dialog, then prove it landed.

        Two traps, both of which produced silent false successes before they
        were handled:

        1. When TikTok's content check has not finished, clicking Post opens a
           **"Continue to post?"** dialog whose confirm button is labelled
           "Post now" (it still honours the Schedule setting — it means
           "proceed", not "publish immediately"). Miss it and the submission is
           abandoned the moment the page closes.
        2. A blind `wait_for_timeout` after the click proves nothing. The form
           only unmounts once TikTok has actually accepted the post, so waiting
           for the schedule radio to detach is the real completion signal.

        Whatever this method decides, `verify` reconciles the plan against the
        account afterwards -- the account is the source of truth, not the click.
        """
        try:
            await self._dismiss(page)
            # TikTok keeps the button disabled until its own upload finishes.
            await page.wait_for_function(
                "() => [...document.querySelectorAll('button')].some("
                "  b => /^(Post|Schedule|\u0110\u0103ng|L\u00ean l\u1ecbch)$/"
                "         .test(b.textContent.trim()) && !b.disabled)",
                timeout=PROCESSING_TIMEOUT_MS,
            )
            await self._dismiss(page)
            if not await self._click_submit(page):
                raise TikTokPublishError("submit", "the Post/Schedule button never enabled")

            # Clear the confirm dialog if it appears, and wait for the form to
            # unmount, which is what "TikTok accepted it" actually looks like.
            confirmed = False
            for _ in range(60):
                await page.wait_for_timeout(1_000)
                if await self._confirm_dialog(page):
                    confirmed = True
                    continue
                if await page.locator('input[name="postSchedule"]').count() == 0:
                    return
                # The button can re-enable if the first click was swallowed.
                if not confirmed and await self._click_submit(page):
                    continue
            raise TikTokPublishError(
                "submit",
                "the upload form never cleared, so TikTok did not accept the post",
                await self._shot(page, slot, "submit"),
            )
        except TikTokPublishError:
            raise
        except Exception as exc:
            raise TikTokPublishError(
                "submit", str(exc), await self._shot(page, slot, "submit")
            ) from exc

    @staticmethod
    async def _click_submit(page: Any) -> bool:
        return await page.evaluate(
            """() => {
              const b = [...document.querySelectorAll('button')].find(
                x => /^(Post|Schedule|\u0110\u0103ng|L\u00ean l\u1ecbch)$/
                       .test(x.textContent.trim()) && !x.disabled);
              if (!b) return false;
              b.click();
              return true;
            }"""
        )

    @staticmethod
    async def _confirm_dialog(page: Any) -> bool:
        """Click through "Continue to post?" if it is showing.

        Matched on the dialog's own question text so a stray "Post now" radio
        elsewhere on the form can never be hit by mistake.
        """
        return await page.evaluate(
            """() => {
              const asking = [...document.querySelectorAll('div,h1,h2,h3,span')].some(
                e => /Continue to post|still checking your video|Ti\u1ebfp t\u1ee5c \u0111\u0103ng/i
                       .test((e.textContent || '').trim().slice(0, 200)));
              if (!asking) return false;
              const b = [...document.querySelectorAll('button')].find(
                x => /^(Post now|Continue|\u0110\u0103ng ngay|Ti\u1ebfp t\u1ee5c)$/
                       .test(x.textContent.trim()) && !x.disabled);
              if (!b) return false;
              b.click();
              return true;
            }"""
        )

    # -- helpers ------------------------------------------------------------ #

    async def _click_any(self, page: Any, labels: list[str], exact: bool = False) -> None:
        last: Optional[Exception] = None
        for label in labels:
            try:
                await page.get_by_text(label, exact=exact).first.click(timeout=6_000)
                return
            except Exception as exc:
                last = exc
        raise TikTokPublishError("click", f"none of {labels} was clickable: {last}")

    async def _shot(self, page: Any, slot: dict[str, Any], step: str) -> str:
        """Screenshot the failure. Upload pages change without notice, and this
        is usually the only evidence of what the page looked like."""
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self.evidence_dir / f"ep{slot.get('ep', 'x')}-{step}.png"
            await page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return ""


CONTENT_URL = "https://www.tiktok.com/tiktokstudio/content"

# The Posts page renders its own list from this endpoint. Reading the JSON it
# already fetches beats scraping the table, which is *virtualised*: only about
# nine rows exist in the DOM at any moment, so `innerText` returns a moving
# slice regardless of how patiently you scroll. That slice is what once
# reported 9 of 44 episodes present and marked the other 35 for re-posting.
ITEM_LIST_PATH = "creator/manage/item_list"

def _episode_matcher(code: str) -> "re.Pattern[str]":
    """Match only *this* series' captions.

    A bare ``T\u1eadp\\s*(\\d+):`` looks right and is not: this account also runs a
    ``#system_design`` series numbered from 1, so the loose pattern reported
    episodes 17-28 as present when the only posts with those numbers belonged to
    the other series. Anchoring on the series code is what makes the count mean
    anything. The bracket is optional so hand-posted episodes ("LC T\u1eadp 5:")
    are recognised alongside the tool's own "[LC] T\u1eadp 5:".
    """
    return re.compile(rf"^\s*\[?{re.escape(code)}\]?\s*T\u1eadp\s*(\d+)\s*:", re.IGNORECASE)


async def _scrape_account(
    account: str, code: str, headless: bool = True
) -> dict[int, list[datetime]]:
    """This series' episodes on the account, each with the times it is set for.

    The account is the only honest source of truth. A click that "worked" can
    still leave nothing behind, so the plan is reconciled against this rather
    than against what the uploader believed it did -- and against the *time*,
    not merely the existence, because a post at the wrong hour is not a success.

    Completeness matters more than freshness: an under-read causes duplicate
    posts. The page is scrolled until the endpoint reports ``has_more: false``,
    and a read that never sees that flag raises rather than returning a partial
    set.
    """
    from playwright.async_api import async_playwright

    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / "Library" / "Caches" / "ms-playwright")
    )
    state = load_storage_state(account)
    matcher = _episode_matcher(code)
    items: dict[Any, dict[str, Any]] = {}
    complete = False

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            context = await browser.new_context(
                storage_state=state, user_agent=USER_AGENT,
                viewport={"width": 1500, "height": 1000},
            )
            page = await context.new_page()
            page.set_default_timeout(90_000)

            async def on_response(response: Any) -> None:
                nonlocal complete
                if ITEM_LIST_PATH not in response.url:
                    return
                try:
                    payload = await response.json()
                except Exception:
                    return
                for item in payload.get("item_list") or []:
                    items[item.get("item_id")] = item
                if payload.get("has_more") is False:
                    complete = True

            page.on("response", on_response)
            await page.goto(CONTENT_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(10_000)

            # Scroll the list's own container — the window does not scroll.
            for _ in range(40):
                if complete:
                    break
                await page.evaluate(
                    """() => {
                      for (const e of document.querySelectorAll('*'))
                        if (e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200)
                          e.scrollTop += 4000;
                    }"""
                )
                await page.wait_for_timeout(2_000)
        finally:
            await browser.close()

    if not complete:
        raise TikTokPublishError(
            "verify",
            f"the account listing never finished loading (saw {len(items)} posts). "
            f"Refusing to reconcile on a partial read, because that would mark "
            f"live posts as pending and re-post them.",
        )

    found: dict[int, list[datetime]] = {}
    for item in items.values():
        hit = matcher.match(item.get("desc") or "")
        if not hit:
            continue
        stamp = item.get("post_time") or item.get("create_time")
        if not stamp:
            continue
        found.setdefault(int(hit.group(1)), []).append(datetime.fromtimestamp(int(stamp)))
    return found


async def _run_slots(
    slots: list[dict[str, Any]],
    account: str,
    visibility: str,
    headless: bool,
    dry_run: bool,
    evidence_dir: Path,
    on_done: Any,
) -> None:
    from playwright.async_api import async_playwright

    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / "Library" / "Caches" / "ms-playwright")
    )
    state = load_storage_state(account)

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            context = await browser.new_context(
                storage_state=state,
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 950},
            )
            uploader = _Uploader(context, evidence_dir, LABELS)
            try:
                for slot in slots:
                    try:
                        result = await uploader.schedule(slot, visibility, dry_run)
                        slot["state"] = result["state"]
                        slot["last_error"] = ""
                        slot["screenshot"] = result.get("screenshot", "")
                        slot["scheduled_at"] = datetime.now().isoformat(timespec="seconds")
                    except Exception as exc:
                        slot["state"] = "failed"
                        slot["last_error"] = str(exc)[:600]
                    slot["attempts"] = int(slot.get("attempts", 0)) + 1
                    # Persist after every slot: a crash costs one episode, not the run.
                    on_done()
                    if slot is not slots[-1]:
                        await asyncio.sleep(6)
            finally:
                await context.close()
        finally:
            await browser.close()


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #

class TikTokPublish(BaseTool):
    name = "tiktok_publish"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "tiktok"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = ["python:playwright"]
    install_instructions = (
        "pip install playwright && playwright install chromium. "
        "No API key: authentication is a cookie jar you export from a browser "
        "extension (Cookie-Editor) while signed in to the TikTok account."
    )

    agent_skills = ["playwright-recording"]

    capabilities = [
        "import_cookies",
        "plan_series_schedule",
        "schedule_upload",
        "rolling_top_up",
    ]
    supports = {
        "local_offline": False,
        "free": True,
        "uploads": True,
        "scheduling": True,
        "max_schedule_days_ahead": SCHEDULE_HORIZON_DAYS,
        "resumable": True,
    }
    best_for = [
        "putting a finished series on a fixed daily posting rhythm",
        "runs longer than TikTok's scheduling window, via a plan plus a top-up",
        "re-runnable publishing where a crash must not double-post",
    ]
    not_good_for = [
        f"runs longer than TikTok's scheduling window (~{SCHEDULE_HORIZON_DAYS} "
        "days) in one shot — the plan file plus top-up exists for this",
        "accounts without Creator/Business mode (the Schedule control is hidden)",
        "posting to platforms other than TikTok",
    ]

    input_schema = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["import_cookies", "plan", "status", "run", "verify"],
                "description": (
                    "import_cookies: store an exported jar. plan: build/refresh the "
                    "schedule file. status: report what is pending/scheduled/failed. "
                    "run: push every due slot inside the horizon. verify: "
                    "reconcile the plan against what the account actually holds."
                ),
            },
            "cookies_path": {"type": "string", "description": "import_cookies: the exported file."},
            "account": {
                "type": "string",
                "description": "Which stored jar to use. Defaults to 'default'.",
            },
            "plan_path": {"type": "string", "description": "Where the schedule JSON lives."},
            "series_code": {"type": "string", "description": "Caption prefix, e.g. 'LC'."},
            "series_id": {"type": "string"},
            "series_title": {"type": "string"},
            "episodes": {
                "type": "array",
                "description": (
                    "plan: [{ep, title, video, slug?}] in posting order. "
                    "`video` is the path to the final render."
                ),
                "items": {"type": "object"},
            },
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "start_date": {"type": "string", "description": "plan: YYYY-MM-DD of the first slot."},
            "times": {
                "type": "array",
                "items": {"type": "string"},
                "description": "plan: daily clock times in order, e.g. ['11:30','19:30'].",
            },
            "timezone_note": {
                "type": "string",
                "description": (
                    "Free text recorded in the plan. TikTok schedules in the "
                    "account's own timezone, not the machine's."
                ),
            },
            "visibility": {"type": "string", "enum": ["public", "unlisted", "private"]},
            "horizon_days": {
                "type": "integer",
                "description": f"run: how far ahead to push. Defaults to {SCHEDULE_HORIZON_DAYS}.",
            },
            "limit": {"type": "integer", "description": "run: cap slots pushed this run."},
            "only": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "run: restrict to these episode numbers.",
            },
            "retry_failed": {"type": "boolean", "description": "run: also retry failed slots."},
            "dry_run": {
                "type": "boolean",
                "description": "run: fill the form and screenshot it, but never press Post.",
            },
            "headless": {"type": "boolean", "description": "run: default false — watch it work."},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "plan_path": {"type": "string"},
            "summary": {"type": "object"},
            "slots": {"type": "array", "items": {"type": "object"}},
            "next_top_up": {"type": "string"},
            "publish_log": {"type": "object"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, backoff_seconds=30.0)
    resume_support = ResumeSupport.FROM_CHECKPOINT
    side_effects = [
        "writes a cookie jar to ~/.openmontage/cookies/ (0600, outside the repo)",
        "writes and rewrites the schedule plan JSON",
        "SCHEDULES REAL PUBLIC POSTS on the connected TikTok account",
    ]
    user_visible_verification = [
        "Open TikTok Studio -> Posts -> Scheduled and confirm the dates, times and captions",
        "Check the first scheduled post's caption reads '[CODE] Tập N: Title' with the hashtag line",
    ]

    # ---- Actions --------------------------------------------------------- #

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        action = inputs.get("action")
        try:
            if action == "import_cookies":
                return self._import_cookies(inputs)
            if action == "plan":
                return self._plan(inputs)
            if action == "status":
                return self._status(inputs)
            if action == "run":
                return self._run(inputs)
            if action == "verify":
                return self._verify(inputs)
            return ToolResult(
                success=False, error=f"unknown action {action!r}; expected one of "
                "import_cookies, plan, status, run"
            )
        except Exception as exc:  # surfaced to the agent, never swallowed
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

    def _import_cookies(self, inputs: dict[str, Any]) -> ToolResult:
        source = Path(inputs["cookies_path"]).expanduser()
        account = inputs.get("account") or "default"
        cookies = parse_cookie_export(source.read_text(encoding="utf-8", errors="ignore"))
        cookies = [c for c in cookies if "tiktok.com" in c["domain"]]
        if not cookies:
            return ToolResult(
                success=False,
                error=f"{source} holds no tiktok.com cookies — is this the right export?",
            )

        summary = summarise_cookies(cookies)
        if not summary["has_sessionid"]:
            return ToolResult(
                success=False,
                error=(
                    "the export has no 'sessionid' cookie, so it cannot authenticate. "
                    "Export again while signed in, with the extension set to show all "
                    "cookies for tiktok.com."
                ),
            )

        target = cookie_path(account)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        target.chmod(0o600)
        return ToolResult(
            success=True,
            data={
                "action": "import_cookies",
                "account": account,
                "stored_at": str(target),
                "summary": summary,
            },
        )

    def _plan(self, inputs: dict[str, Any]) -> ToolResult:
        episodes = inputs.get("episodes") or []
        if not episodes:
            return ToolResult(success=False, error="plan needs a non-empty `episodes` list")

        code = inputs.get("series_code") or ""
        hashtags = inputs.get("hashtags") or []
        times = inputs.get("times") or ["11:30"]
        when = slot_datetimes(inputs["start_date"], times, len(episodes))

        plan_path = Path(inputs["plan_path"]).expanduser()
        existing = self._load_plan(plan_path)
        already = {
            int(s["ep"]): s
            for s in (existing.get("slots") if existing else [])
            if s.get("state") == "scheduled"
        }

        slots = []
        missing_video = []
        for episode, moment in zip(episodes, when):
            ep = int(episode["ep"])
            if ep in already:
                # Never re-plan something already live on TikTok's scheduler.
                slots.append(already[ep])
                continue
            video = Path(episode["video"]).expanduser()
            if not video.is_file():
                missing_video.append(f"ep{ep}: {video}")
            slots.append(
                {
                    "ep": ep,
                    "slug": episode.get("slug", ""),
                    "title": episode["title"],
                    "video": str(video),
                    "caption": build_caption(code, ep, episode["title"], hashtags),
                    "scheduled_for": moment.isoformat(timespec="minutes"),
                    "state": "pending",
                    "attempts": 0,
                    "last_error": "",
                    "scheduled_at": None,
                }
            )

        plan = {
            "version": "1.0",
            "series": {
                "id": inputs.get("series_id", ""),
                "code": code,
                "title": inputs.get("series_title", ""),
                "account": inputs.get("account") or "default",
                "timezone_note": inputs.get(
                    "timezone_note",
                    "TikTok schedules in the account's own timezone (Settings -> Time zone).",
                ),
            },
            "hashtags": hashtags,
            "visibility": inputs.get("visibility", "public"),
            "caption_format": "[{code}] Tập {ep}: {title}\\n{hashtags}",
            "schedule": {
                "start_date": inputs["start_date"],
                "times": times,
                "horizon_days": SCHEDULE_HORIZON_DAYS,
            },
            "created": (existing or {}).get("created")
            or datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "slots": slots,
        }
        _atomic_write_json(plan_path, plan)

        return ToolResult(
            success=True,
            data={
                "action": "plan",
                "plan_path": str(plan_path),
                "summary": self._summarise(plan),
                "missing_video": missing_video,
                "first_slot": slots[0]["scheduled_for"] if slots else None,
                "last_slot": slots[-1]["scheduled_for"] if slots else None,
                "sample_caption": slots[0]["caption"] if slots else "",
            },
        )

    def _status(self, inputs: dict[str, Any]) -> ToolResult:
        plan_path = Path(inputs["plan_path"]).expanduser()
        plan = self._load_plan(plan_path)
        if plan is None:
            return ToolResult(success=False, error=f"no plan at {plan_path}")
        summary = self._summarise(plan)
        return ToolResult(
            success=True,
            data={
                "action": "status",
                "plan_path": str(plan_path),
                "summary": summary,
                "due_now": [s["ep"] for s in self._due(plan, inputs)],
                "next_top_up": summary["next_top_up"],
                "failed": [
                    {"ep": s["ep"], "error": s.get("last_error", "")}
                    for s in plan["slots"]
                    if s.get("state") == "failed"
                ],
            },
        )

    def _run(self, inputs: dict[str, Any]) -> ToolResult:
        plan_path = Path(inputs["plan_path"]).expanduser()
        plan = self._load_plan(plan_path)
        if plan is None:
            return ToolResult(success=False, error=f"no plan at {plan_path}")

        due = self._due(plan, inputs)
        limit = inputs.get("limit")
        if limit:
            due = due[: int(limit)]
        if not due:
            summary = self._summarise(plan)
            return ToolResult(
                success=True,
                data={
                    "action": "run",
                    "plan_path": str(plan_path),
                    "pushed": 0,
                    "summary": summary,
                    "next_top_up": summary["next_top_up"],
                    "note": "nothing due inside the horizon — this is the normal quiet run",
                },
            )

        account = plan["series"].get("account") or inputs.get("account") or "default"
        evidence = plan_path.parent / "publish-evidence"

        def persist() -> None:
            plan["updated"] = datetime.now().isoformat(timespec="seconds")
            _atomic_write_json(plan_path, plan)

        asyncio.run(
            _run_slots(
                due,
                account=account,
                visibility=plan.get("visibility", "public"),
                headless=bool(inputs.get("headless", True)),
                dry_run=bool(inputs.get("dry_run", False)),
                evidence_dir=evidence,
                on_done=persist,
            )
        )
        persist()

        summary = self._summarise(plan)
        scheduled = [s for s in due if s["state"] in ("scheduled", "dry_run")]
        failed = [s for s in due if s["state"] == "failed"]
        return ToolResult(
            success=not failed,
            error="; ".join(f"ep{s['ep']}: {s['last_error']}" for s in failed) or None,
            data={
                "action": "run",
                "plan_path": str(plan_path),
                "pushed": len(scheduled),
                "slots": [
                    {
                        "ep": s["ep"],
                        "state": s["state"],
                        "scheduled_for": s["scheduled_for"],
                        "error": s.get("last_error", ""),
                        "screenshot": s.get("screenshot", ""),
                    }
                    for s in due
                ],
                "summary": summary,
                "next_top_up": summary["next_top_up"],
                "publish_log": self._publish_log(plan, due),
            },
        )

    def _verify(self, inputs: dict[str, Any]) -> ToolResult:
        """Correct the plan from the account. Believe TikTok, not the uploader.

        A slot counts as done only when the account holds a post for this series
        with that episode number **at the planned minute**. Presence alone is
        not success: a post sitting at the wrong hour is a defect the runner
        cannot see.
        """
        plan_path = Path(inputs["plan_path"]).expanduser()
        plan = self._load_plan(plan_path)
        if plan is None:
            return ToolResult(success=False, error=f"no plan at {plan_path}")

        series = plan["series"]
        account = series.get("account") or inputs.get("account") or "default"
        code = series.get("code") or ""
        if not code:
            return ToolResult(
                success=False,
                error="the plan has no series code, so posts cannot be told apart "
                      "from other series on the same account",
            )
        on_account = asyncio.run(
            _scrape_account(account, code, headless=bool(inputs.get("headless", True)))
        )

        previously = set(plan.get("verified", {}).get("on_account", []))
        confirmed, corrected, wrong_time, disappeared = [], [], [], []
        for slot in plan["slots"]:
            ep = int(slot["ep"])
            want = datetime.fromisoformat(slot["scheduled_for"])
            times = on_account.get(ep, [])
            matches = [t for t in times if t.strftime("%Y-%m-%d %H:%M") == want.strftime("%Y-%m-%d %H:%M")]

            if matches:
                slot["state"] = "scheduled"
                slot["last_error"] = ""
                confirmed.append(ep)
            elif times:
                # The episode is up, but not when it was meant to be. Left as-is
                # rather than reset — re-running would post it a second time.
                wrong_time.append(
                    {"ep": ep, "wanted": want.isoformat(timespec="minutes"),
                     "found": [t.isoformat(timespec="minutes") for t in times]}
                )
            elif slot["state"] == "scheduled":
                if ep in previously:
                    # Seen on the account before and absent now. A deleted post is
                    # possible; a short read is likelier, and resetting would
                    # duplicate a live post. Flag, never reset.
                    disappeared.append(ep)
                else:
                    slot["state"] = "pending"
                    slot["last_error"] = (
                        "reported scheduled but absent from the account - reset to pending"
                    )
                    corrected.append(ep)

        plan["verified"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            # Union, not replacement: the high-water mark of what the account has
            # ever been seen to hold, which is what makes the rule above work.
            "on_account": sorted(previously | set(on_account)),
        }
        plan["updated"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(plan_path, plan)

        return ToolResult(
            success=not (corrected or wrong_time),
            error="; ".join(
                [f"ep{e} absent" for e in corrected]
                + [f"ep{w['ep']} at {w['found']} not {w['wanted']}" for w in wrong_time]
            ) or None,
            data={
                "action": "verify",
                "plan_path": str(plan_path),
                "series_code": code,
                "episodes_on_account": sorted(on_account),
                "confirmed_at_planned_time": sorted(confirmed),
                "wrong_time": wrong_time,
                "reset_to_pending": sorted(corrected),
                "disappeared_not_reset": sorted(disappeared),
                "summary": self._summarise(plan),
            },
        )

    # ---- Helpers ---------------------------------------------------------- #

    @staticmethod
    def _load_plan(path: Path) -> Optional[dict[str, Any]]:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _due(plan: dict[str, Any], inputs: dict[str, Any]) -> list[dict[str, Any]]:
        """Slots that are still owed and close enough for TikTok to accept."""
        horizon_days = int(inputs.get("horizon_days") or SCHEDULE_HORIZON_DAYS)
        cutoff = datetime.now() + timedelta(days=horizon_days)
        now = datetime.now()
        only = {int(e) for e in (inputs.get("only") or [])}
        retry = bool(inputs.get("retry_failed"))

        # `dry_run` is a rehearsal, not an outcome — a real run must still owe it.
        wanted = {"pending", "dry_run"} | ({"failed"} if retry else set())
        out = []
        for slot in plan["slots"]:
            if slot.get("state") not in wanted:
                continue
            if only and int(slot["ep"]) not in only:
                continue
            moment = datetime.fromisoformat(slot["scheduled_for"])
            # A slot whose time has passed can no longer be scheduled; leave it
            # visible as pending rather than silently posting it late.
            if moment <= now or moment > cutoff:
                continue
            out.append(slot)
        return sorted(out, key=lambda s: s["scheduled_for"])

    @staticmethod
    def _summarise(plan: dict[str, Any]) -> dict[str, Any]:
        slots = plan["slots"]
        states: dict[str, int] = {}
        for slot in slots:
            states[slot.get("state", "pending")] = states.get(slot.get("state", "pending"), 0) + 1

        pending = [s for s in slots if s.get("state") == "pending"]
        now = datetime.now()
        cutoff = now + timedelta(days=SCHEDULE_HORIZON_DAYS)
        # The next date on which a still-pending slot enters the horizon — i.e.
        # the day the top-up run has something to do again.
        future = [
            datetime.fromisoformat(s["scheduled_for"])
            for s in pending
            if datetime.fromisoformat(s["scheduled_for"]) > cutoff
        ]
        next_top_up = (
            (min(future) - timedelta(days=SCHEDULE_HORIZON_DAYS)).date().isoformat()
            if future
            else None
        )
        missed = [
            s["ep"]
            for s in pending
            if datetime.fromisoformat(s["scheduled_for"]) <= now
        ]
        return {
            "total": len(slots),
            "by_state": states,
            "horizon_days": SCHEDULE_HORIZON_DAYS,
            "in_horizon_pending": sum(
                1
                for s in pending
                if now < datetime.fromisoformat(s["scheduled_for"]) <= cutoff
            ),
            "next_top_up": next_top_up,
            "missed_slots": missed,
            "first_pending": min((s["scheduled_for"] for s in pending), default=None),
            "last_slot": max((s["scheduled_for"] for s in slots), default=None),
        }

    @staticmethod
    def _publish_log(plan: dict[str, Any], slots: list[dict[str, Any]]) -> dict[str, Any]:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        entries = []
        for slot in slots:
            state = slot.get("state")
            entries.append(
                {
                    "platform": "tiktok",
                    "status": "scheduled" if state == "scheduled" else (
                        "draft" if state == "dry_run" else "failed"
                    ),
                    "visibility": plan.get("visibility", "public"),
                    "timestamp": stamp,
                    "scheduled_for": datetime.fromisoformat(
                        slot["scheduled_for"]
                    ).astimezone().isoformat(timespec="seconds"),
                    "metadata_used": {
                        "title": slot["title"],
                        "description": slot["caption"],
                        "hashtags": plan.get("hashtags", []),
                    },
                    **({"error": slot["last_error"]} if slot.get("last_error") else {}),
                }
            )
        return {"version": "1.0", "entries": entries}

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        # Measured shape: ~70-110s per episode, dominated by TikTok's own
        # transcode of a 4-6 minute 1080x1920 file before Post enables.
        return 90.0 * max(1, int(inputs.get("limit") or 1))

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Report what `run` would push, without opening a browser."""
        plan_path = Path(inputs.get("plan_path", "")).expanduser()
        plan = self._load_plan(plan_path)
        if plan is None:
            return {"ok": False, "reason": f"no plan at {plan_path}"}
        due = self._due(plan, inputs)
        return {
            "ok": True,
            "would_push": [
                {"ep": s["ep"], "scheduled_for": s["scheduled_for"], "caption": s["caption"]}
                for s in due
            ],
            "summary": self._summarise(plan),
        }
