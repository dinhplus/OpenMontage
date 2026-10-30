# Context Economics

Meta-skill for **long production runs that would otherwise fill the context window and
hang**. Read this when a job spans many similar deliverables — a series, a batch
migration, a multi-file audit.

Evidence base: a session that hung on a full context window after two episodes, was
instrumented, and then produced thirteen more in the same session.

## Measure first — the obvious culprit was wrong

The transcript is a JSONL file on disk. Parse it before theorizing:

```python
# ~/.claude/projects/<project>/<session-id>.jsonl
# One JSON object per line; assistant turns carry content blocks of
# type text | thinking | tool_use | tool_result.
```

Attribute by source and convert honestly: text is roughly chars/4, but **images are
~1.6k tokens each regardless of file size**. Counting base64 characters will tell you
images are 60% of your context when they are 13%.

First measurement produced this, for ~409k tokens across two episodes:

| source | tokens | share |
|---|---:|---:|
| Bash INPUT (inline Python heredocs) | 120k | 29% |
| Write INPUT (actual deliverables) | 90k | 22% |
| QA images (32 separate stills) | 51k | 13% |
| Bash OUTPUT (verbose logs) | 50k | 12% |
| Read (guide + stage skills) | 44k | 11% |

The obvious read — "the heredocs are the problem, replace them with Write" — was
**wrong, and would have made it worse.** Dissecting the largest heredoc showed it was
**95% content, 5% boilerplate**, and that generating an artifact programmatically cost
~4k tokens where writing the same 36kB artifact directly would cost ~9k.

**The repetition is not in generating content. It is in the mechanics around content:**
scaffolding, staging, cache clearing, manifest building, checkpoint writing, packaging.
Six such steps × N deliverables is where the window goes.

## Extract mechanics to commands, keep generation as scripts

Every step that is *derivable from artifacts already on disk* becomes a subcommand:

```
ep.py check <slug> <stage>     structural validation, writes nothing
ep.py close <slug> <stage>     reads artifact from disk, embeds into checkpoint
ep.py wire <slug> ...          builds manifest + edit decisions, closes two stages
ep.py scaffold / props / stage
captions.py <slug>
finish.py <slug> ...           render → master → verify → reports → package → publish
```

One deliverable went from a series of long heredocs to seven one-line commands. The
checkpoint-embedding case is worth naming: checkpoints wrap the full artifact, so
writing both inline means **paying for the content twice in one command**. Having the
tool read it from disk removes one whole copy.

## The four output-side savings

**Contact sheets for visual QA.** Tile 6 frames into one image with guide lines drawn
on (safe area, caption band). 51k → ~10k tokens, and reviewing scenes side by side
catches rhythm problems that scene-by-scene review cannot.

**Cap command output.** `2>&1 | tail -5` or `>/dev/null && echo ok`. Successful
renders and ffmpeg runs carry no information in their logs.

**One playbook file replaces re-reading the guide.** After compaction you would re-read
the onboarding guide plus every stage-director skill — 44k tokens to relearn decisions
you already made. A single file of *settled decisions and traps already paid for* costs
~5k.

**Never re-read a file you just wrote.** Write/Edit error out if they fail.

## Two traps that made me report a failure as a success

**Do not truncate error messages.** `str(err)[:300]` on a subprocess failure showed the
command line and cut off the actual cause. Diagnosis started from zero. Write the full
error to a log file and print the *tail*, not the head — the cause is at the end.

**Do not wrap a long command in `| grep | tail`.** The pipe returns grep's exit code,
so a failed job reports success. This is how I told the user an episode was rendering
when it had already crashed. Redirect to a log, then `echo $?`.

## Delegation is context economics, not just parallelism

A subagent's context is not yours. Work whose *input* is large but whose *output* is
small is the ideal candidate: research (34k of search results → a 2k brief), metadata
writing, code authoring you will review by looking at rendered output rather than by
reading the code.

Keep for yourself only what genuinely needs your judgment. In this run that was two
things: reading scripts for voice consistency, and reading QA contact sheets for layout
defects. Everything else went behind a command or into an agent.

## Know when to stop, and stop at a clean boundary

Measured cost was ~200k tokens per deliverable, mostly irreducible authoring. No amount
of tooling fits six of those in one window.

So: **carry one deliverable all the way to done rather than opening all of them
halfway.** If you run out mid-run, what remains is a clean boundary and a playbook,
not N half-finished items. State this to the user early — it is a scheduling fact, not
a limitation to apologize for.

Do sample rather than skip when you must reduce: reviewing 6 of 24 scenes is a stated
coverage reduction; skipping review entirely is a silent one. **Say which you did.**

## Related

- `skills/meta/multi-agent-production.md` — what to delegate and how to brief it
- `skills/meta/checkpoint-protocol.md` — the stage gates these commands must respect
