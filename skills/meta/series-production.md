# Series Production

Meta-skill for producing a **multi-episode series** — many videos that must look like they
come from the same house, hold the same editorial standard, and survive later format
changes as a set.

A series is not N videos. It is one design system, one editorial standard, and one
timeline, instantiated N times. Almost every expensive failure in this skill comes from
forgetting that and treating an episode as standalone.

Evidence base: a 28-episode Vietnamese System Design series, three seasons, produced and
then converted from 16:9 to 9:16 in a single session. Numbers below are measured, not
estimated.

## Rule: the design system is the only place a shared decision may live

Every episode gets a **copy** of the shared components (`sync.py` writes them into each
episode's `composition/`). Edit only the source in `_sd-series/design-system/`, then sync.
Editing a copy produces an episode that silently drifts from the other twenty-seven.

Build the sync into the render preflight so it cannot be forgotten:

```python
# ep.py _preflight — runs before every stage
# 1. byte-compare files copied verbatim from the reference episode
# 2. run sync.py for files that pass through a transform
```

Forgetting `sync.py` between a design-system edit and a render is a real, repeated failure:
the render succeeds, output is byte-identical to the previous one, and nothing warns you.
It was caught only by diffing QA images.

## The shared-component drift class — eight instances, one shape

When the series changed aspect ratio, **eight shared components** turned out to hardcode
landscape-only parameters. None threw an error. None failed a schema check. All rendered
successfully and looked wrong.

| Component | What it hardcoded | Why it was missed |
|---|---|---|
| `EpisodeCard` | number 150px, title 50px | no progress bar |
| `InterviewerHears` | tag 16px, body 27px | no progress bar |
| `NextHook` | question 62px, label 22px, padding 140px | no progress bar |
| `Stage` | `SERIES.safeTop/safeBottom` | no progress bar |
| `Box` | label 21px, sub **15px** | no progress bar |
| `Stat` | caption 21px | no progress bar |
| caption pagination | 5 words/page | lives in `Main.tsx`, not the design system |
| `Arrow` | arrowhead offset applied *before* rotation | only wrong for vertical arrows |

**The pattern is not random.** Three separate "portrait audit" passes fixed only the
components with progress bars, because that is how the auditor found things — by looking at
the devices they remembered. Everything without a progress bar was never opened.

**How to actually audit:** enumerate the export list of the design system and check every
symbol, not the ones you can recall. `grep -c "tall\|H > W"` per component is a two-minute
sweep that would have found all eight on day one.

The worst of the eight is instructive: `Stage` exists to keep content out of the caption
band, and it placed its own bottom edge at `SERIES.safeBottom` = 0.80H — which is exactly
`PORTRAIT.captionTop`. The guard rail was the collision.

## Aspect-ratio conversion — the one sentence that explains all of it

> **Text keeps its absolute width. The frame narrows by 44%. Every gap expressed as a
> fraction shrinks; the text does not.**

A 16:9 layout that looks "roomy" is not roomy. It only means the strings were not yet long
enough to touch **at that width**.

Consequences, each of which cost a fix:

- A label centred at `W/2` clears a bar at `0.60W` in landscape and lands on it in portrait.
- A stamp placed at `0.76W` sits in white space when the quote box is 1690px wide and on
  top of the last word when the box is 950px.
- A 19-character label starting at `0.50W` occupies 0.17W of a 1920 frame and 0.30W of a
  1080 frame — swallowing the label next to it.
- The band `0.13H–0.22H` is where the section heading and the first diagram element collide.
  Check it explicitly; it broke in four separate episodes.

### Two rules that prevent most of it

**Anchor to elements, never to constants.** `y={H * 0.218}` is correct until someone moves
the box it annotates. `y={box[1] + boxH/2 + 18}` stays correct. Three fixes in this session
were caused by a label left behind when its box moved — including one where *fixing* the
collision created a new one.

**Measure from the rendered image; do not estimate.** Estimating text width was wrong every
single time it was tried — by 30–40%, in both directions. Five geometry fixes missed on the
first attempt for this reason and needed a second pass. Rendering one scene at `--scale 0.75`
and reading coordinates off it costs ~20 seconds and is right the first time.

### Converting an existing series

Do **not** flip the format constant and re-render. That produces a landscape video letterboxed
by neglect: nothing overflows, but content occupies ~55% of the height and every horizontal
row is crushed to 44% width.

Rewrite each episode's `scenes.tsx` for the new aspect. Line counts grew 12–80% — the signal
that layout was genuinely rebuilt rather than rescaled. Horizontal timelines become vertical
rails; four-across card rows become stacked lists; left-right comparisons become top-bottom.

Preserve everything else: script, narration audio, captions, scene timings. Only the
composition changes. `scaffold` regenerates the format wiring and leaves `scenes.tsx` alone.

## QA boards are the only step that cannot be delegated

`qa.py` renders one sampled frame per scene into contact-sheet images. Reading them is the
sole quality gate that catches what no tool does.

In this session the boards caught, among others:
- a season-opener listing **nine** of its season's **ten** episodes
- captions falling below their own band because one episode was scaffolded before a fix
- a source label rendered on top of the bar it attributed
- a ratio label swallowed by the bar it described

None of these fail `hookcheck`, schema validation, scene-key comparison, or the render itself.

**Budget for false alarms.** Roughly half of what looks broken on a board is a mid-animation
frame: a counter still counting, a block sliding in, a previous layer dimming out, a chip that
appears two seconds later. Seven such suspicions in one session were all benign.

**The only reliable way to tell them apart is to read the code**, not to look harder at the
image. A vertical spine crossing a word may be a bug or may be the whole point — the comment
above it will say which.

**Trap:** `qa.py --scenes` overwrites the same filenames as the full board. Zooming into one
scene destroys the evidence for the others. Re-render the full set, or teach the tool to
write distinct filenames.

## Fact integrity — a source label is not evidence

Two episodes, two seasons apart, failed the same way: a number carried a `primary_source`
attribution, and the cited page did not contain the number.

The worse of the two is worth stating exactly. The research brief attributed three figures to
a page. Fetching the raw HTML and counting occurrences: **zero matches for any of them.** The
page went further and refused to supply the very figure being attributed to it —
*"we have no published measurement … treat the size of that gain as workload-specific rather
than adopting a headline number."* The episode had already shipped, and the number was in the
video and in the published description.

That same beat also rested on a premise that is physically impossible — a 70B model at bf16
needs ~140 GB and the stated single 80 GB accelerator cannot hold it. No amount of arithmetic
checking finds that; it needs someone who knows the domain to read the sentence.

**Cheap mechanical check worth building:** for every sourced number in `research_brief.json`,
fetch the source and count the literal string. This catches the whole class without
comprehension. It would have caught both instances.

**Other failure shapes seen:**
- A range collapsed to a point: brief said "236–335", script read the low end but kept the
  ratio computed from the high end. Produced a claim that was self-inconsistent by 45%.
- A derived number with no source sitting beside sourced ones — viewers cannot tell them apart.
- Two numbers correct, the causal link between them invented: the study said one vendor
  responded to the report and the other explicitly denied it; the episode implied both did.

**When two places disagree, do not default to internal consistency.** Ask which one matches
the outside world, then move the other. An agent "fixed" a description by reverting a correct
number to match a stale one — restoring consistency while keeping both wrong.

## Orchestration for a series

Read `skills/meta/multi-agent-production.md` for the T-shape. Series-specific additions:

**Idle is not done.** A subagent going idle means its turn ended, not that its work landed.
Verify by inspecting what it wrote to disk. This was misread three times in one session:
once spawning a duplicate agent onto a live file (two agents overwrote each other), once
nearly restarting a render that was mid-master, once nudging an agent that had finished.

**`ListAgents` does not list your own subagents.** An empty result proves nothing about them.

**One writer per file, and say who it is.** The lead is not exempt: at one point the lead
patched `scenes.tsx` while an agent it had assigned to the same file was still working.

**Require agents to end turns explicitly.** A turn that stops silently while awaiting a
decision is indistinguishable from a turn that finished or died. Ask for: *"waiting on X;
I have / have not written to disk."*

**Tell agents to doubt the brief.** Instruct them to report contradictions rather than
guess. This is where the highest-value findings came from — the design-system font-size
bug, the `Arrow` rotation bug, the `Stage` coordinate-origin trap, and the unsourced
number — all surfaced by agents that were *forbidden* from fixing the thing they found.

**Composers must not run `stage`, `props`, or `qa.py`** — they clear a shared Remotion cache
and will break a render in progress. Renders stay strictly serial; composition runs parallel.

## Continuity is content, and it rots

A series accumulates statements about itself that quietly become false:

- Episode 1 said "seven episodes to come" — true for one season, wrong for three.
- Episode 8 said "**End of series.** Eight episodes" — it is the end of season one.
- Descriptions cited three different series lengths (8, 18, 28) depending on when written.
- A season-opening scene listed nine of its ten episodes, with the finale missing.

Grep the whole corpus for self-referential counts before publishing anything new. Prefer
phrasing that does not need updating ("end of season one") over phrasing that does
("end of series").

Auto-generate anything indexed by episode number. A hand-written 8-element array silently
produced blank episode numbers for episodes 9–18 when the second season arrived.

## Voice: Chirp3-HD is nondeterministic

See `memory: chirp3-tts-stall`. Series-specific consequences:

- Generate each section **6–8 times** and keep the shortest. Four is not enough — a whole
  batch can land in a slow cluster, and identical md5s within a batch look exactly like an
  API cache but are not one.
- To judge whether edited copy got longer, **re-record the old text in the same batch** and
  compare floor to floor. An archived duration may itself be a lucky take: one measured
  21.7s on disk versus a 22.1s floor across eight fresh takes of the same words.
- Re-recording one section shifts every later timestamp. Expect to re-time all sections,
  regenerate captions, and re-check that no `enhancement_cue` falls outside its window.

## Guards worth building into the tooling

Each of these came from a failure that cost a full render cycle:

| Guard | Catches |
|---|---|
| conditional-hook checker before render | React #300 mid-render (killed one episode at 13s) |
| `SCENES` keys vs `scene_plan` ids | blank scenes discovered after rendering |
| staged copy byte-compare vs source | rendering yesterday's composition |
| shared framework files vs reference | episode scaffolded before a design-system fix |
| `edit_decisions.json` exists before `props` | a late, bare `FileNotFoundError` after a full QA cycle |
| caption text sequence vs script words | stale captions that pass a word **count** check |

### A preflight guard protects the next render, not the last one

The staged-copy and framework-file guards fire when you *start* a render. They know nothing
about episodes already on disk. Fix a shared component today and every episode rendered
before today is silently stale — it passed its own preflight, back when the file still matched.

This bit at the very end of the session: after re-rendering ten episodes for a component fix,
a sweep found **five more** episodes still carrying the pre-fix `Main.tsx`. They had rendered
earlier in the same session, cleanly, under a preflight that was correct at the time.

Make this a fixed step, not an afterthought — it takes two seconds:

```bash
for d in projects/<series>-ep*/; do
  cmp -s projects/<reference-episode>/composition/Main.tsx "$d/composition/Main.tsx" \
    || echo "$d stale"
done
```

Run it after **every** design-system change, and again before declaring a batch complete.
Verifying the output is a separate act from verifying the process that made it.

Two meta-lessons about the guards themselves:

**Never swallow output.** `cmd >/dev/null 2>&1` and `cmd | grep | tail` both destroy the
exit code and the error. One episode rendered in the wrong aspect ratio because a scaffold
failure was discarded this way — and this exact mistake was made, fixed, documented, and
then repeated later in the same session.

**An empty signal is not evidence.** No output, no process, an empty agent list — none of
these prove anything finished or died. Check the artifact on disk.

## Related

- `skills/meta/multi-agent-production.md` — parallel agents, the T-shape
- `skills/meta/context-economics.md` — measuring before optimising
- `skills/meta/gpu-acceleration.md` — render and transcription device choices
- `projects/_sd-series/PLAYBOOK.md` — the concrete per-episode command sequence
