# Multi-Agent Production

Meta-skill for running a **multi-episode series (or any batch of similar deliverables)**
across parallel subagents without losing quality or corrupting shared state.

Evidence base: one session that produced 15 five-minute explainer videos across two
seasons of an 18-episode series, using ~40 subagents. Every number below was measured,
not estimated.

## The shape: fan out wide, narrow in the middle, serialize at the end

Not all stages parallelize. Sort by **what resource the stage consumes**:

| stage | resource | run it |
|---|---|---|
| research | network I/O | **parallel, one agent per deliverable** |
| script / voice-critical authoring | your judgment | **yourself**, or agents you review closely |
| asset generation (TTS, downloads) | remote API | parallel, but stagger rate-limited providers |
| composition authoring (code) | agent context | parallel — agents burn their own context |
| **render** | all CPU cores + a **shared build cache** | **strictly serial** |

The wins are not symmetric. Parallel research is worth it less for wall-clock than
because **34k tokens of raw search results stay in the agent's context instead of
yours**. Ten research agents kept ~340k tokens out of the main thread.

## Shared-resource hazards — the failure that actually bit

Parallel agents each doing correct work can still destroy each other through a resource
neither one owns.

**Real failure:** the composition brief told each agent to run a staging command to
verify its own work. That command did `rm -rf node_modules/.cache`. Two agents ran it
while a third episode was mid-render. The render died 13 minutes in.

Before you fan out, enumerate what the stage touches outside its own directory:

- build caches, lock files, generated bundles
- session-wide API quotas (see below)
- anything a "verify your work" step would rebuild

Then **write the constraint into the brief file, not into your memory.** A constraint
you hold in your head does not survive your own context compaction.

## Session-wide quotas are shared, and they run out mid-flight

WebSearch has a per-session budget shared across *all* agents. Ten research agents
exhausted 200/200 calls; the last six got nothing.

When an agent reports a quota wall:

1. **Do not raise the limit because an agent asked.** Config is the user's decision.
   Surface it; don't self-authorize.
2. Broadcast the workaround to every sibling agent immediately, so they don't each
   rediscover the wall and burn turns on it.
3. **Never leave a hard quota in the brief when the tool is degraded** — "≥15 data
   points" plus no search tool is a recipe for invented data. Say explicitly: the
   threshold is a target, not a quota; drop claims you cannot source; record the
   shortfall in metadata.

Counterintuitive result: losing search **raised** source quality. Agents fell back to
fetching primary sources directly instead of skimming snippets — 41 data points at
90% primary sources, versus ~20 at 70% with search available.

## Give agents the job of doubting the brief

The highest-value pattern found. Instruct research agents to **verify the numbers in
their own assignment** and report back if they cannot be sourced.

Result: **five fabricated numbers caught**, none by self-review. Every one had been
written by the orchestrator (me) into the series bible and would have shipped.

One agent went further and caught that a *search-result summary* had hallucinated an
attribution — the summary credited a statistic to a site that, when fetched directly,
contained no such number. **A snippet can invent a citation.** This is the strongest
argument for "fetch the source, don't trust the summary."

Phrase it as permission, not as a test: *"concluding that nothing better exists is a
valid answer, not a failure."* Otherwise agents pad to look successful.

## The report-back channel must exist, or warnings die in place

Subagents are usually forbidden from writing shared files (correct — otherwise they
collide). But that means **a warning an agent discovers has nowhere to go.**

Real failure: a research agent proved an assignment's hook was half-fabricated and
wrote the warning into its own output artifact. The downstream authoring agent read the
hook from the *shared bible*, which still had the bad version. The agent flagged this
itself; without that, the fabrication would have reached the finished video.

Close the loop both ways:

- **Agents → orchestrator:** brief says "if you find something requiring a change to a
  shared file, report it and wait — do not edit it yourself."
- **Orchestrator → downstream agents:** the brief for the *next* stage must require
  reading the previous stage's `metadata` warnings, not just its main payload.

Then propagate confirmed findings into the shared file yourself, with a
`known_sourcing_issues`-style block that later stages are required to check. Evidence
it works: a number rejected in episode 1 did not reappear in episode 18, seventeen
episodes and a different agent later.

## Briefs on disk, not in prompts

Write one `AGENT_BRIEF.md` per stage and give each agent a 3-line prompt pointing at
it plus its per-item specifics. Cheaper for you, and — more important — **you can fix
the brief mid-flight** and every agent spawned after inherits the fix.

Put per-item specs in a data file (the series bible) so prompts carry only what is
genuinely unique: continuity with the previous item, and per-item hazards.

## Warn about specific past failures, not general principles

"Write correct React" produces nothing. This produced a 5-for-5 clean rate:

> Do not call hooks after `?`, `&&`, `||`, or inside `.map()`. Episode 4 died at second
> 13 from exactly this; episode 15 was caught by preflight.

A named, dated failure with a mechanism is instruction. A principle is decoration.

## The recurring bug class: shared state changed between two of your own steps

Three separate incidents in one run, all the same shape. **You read or copy shared
state, do something slow, and act on it — but an agent changed it in between.**

| what changed | between | symptom |
|---|---|---|
| build cache deleted | stage → render | render died 13 min in |
| `scenes.tsx` edited by its agent | stage → render | video rendered from the **older** version, silently |
| composition still being written | agent "looks done" → render | 9 of 24 scenes; the rest would render blank |

None of these throws an error. All three produce plausible-looking output.

Defend at the **last step before the expensive operation**, not at the first:

- **Re-verify freshness immediately before rendering.** Compare the staged copy byte-for-byte
  against source; abort with the fix command if they differ. Checking at stage time is
  useless — the window is between stage and render.
- **Count, don't eyeball.** A composition at 750 lines vs a sibling at 1390 reads as
  "written more concisely". Counting scene keys against the plan showed 9 of 24. Line
  count, file size, and "the agent said it was done" are all soft signals; an explicit
  key-by-key comparison against the spec is the only hard one.
- **An idle notification is not a completion signal.** It means the agent stopped, not
  that it finished. Verify the artifact against its spec every time.

## Verify mid-flight corrections actually landed

When you message a running agent with a correction, **check the artifact afterwards**.
Do not assume delivery. A one-line grep for the removed phrase is enough, and it caught
that a correction *had* landed in one case where the timing was very tight.

Also check whether an agent edited its files *after* you staged them — compare
checksums between source and staged copy before rendering.

## Related

- `skills/meta/context-economics.md` — measuring where context actually goes
- `skills/meta/gpu-acceleration.md` — verifying an acceleration before adopting it
- `skills/meta/checkpoint-protocol.md` — stage ordering and gates
