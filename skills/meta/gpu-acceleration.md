# GPU Acceleration (Apple Silicon)

Meta-skill for **deciding whether a GPU path is worth adopting** in the render and
transcription stages. The method matters more than the numbers: a faster path that
changes the output is not a win, and you cannot tell which you have without measuring
the output.

Evidence base: measured on an M3 Max (14 CPU cores, 30 GPU cores) against a series
where 15 episodes had already shipped on the CPU path — so any pixel drift would show
as inconsistency between episodes.

## Rule: benchmark wall time and output fidelity in the same run

A benchmark that reports only seconds will happily recommend a change that alters every
frame. Pair every timing with a comparison against the current output:

- **video** — SSIM against the baseline render (`ffmpeg -lavfi ssim`), plus file size
- **transcription** — not WER against the audio, but the **difference in the artifact
  your pipeline actually consumes** (see below)

`projects/_sd-series/bench_gpu.py` implements the video half: renders the same frame
range under each config, times it, and SSIMs each result against the first.

## Measured: Remotion rendering

Two independent knobs, measured separately because they affect different stages:

| config | time | vs base | size | SSIM | verdict |
|---|---:|---:|---:|---:|---|
| default | 6.5s | 1.00× | 0.77 MB | — | baseline |
| `--gl=angle-egl` | 5.6s | **1.16×** | 0.77 MB | **1.000000** | **adopt** |
| `--hardware-acceleration=required` | 5.6s | 1.15× | **1.95 MB** | 0.999754 | reject |
| both | 5.7s | 1.14× | 1.95 MB | 0.999754 | reject |

**`--gl` changes rasterization** (how frames are drawn). `angle-egl` was 16% faster and
**pixel-identical** — SSIM exactly 1.000000. Zero risk of visual drift across episodes,
so adopt.

**`--hardware-acceleration` changes encoding** (frames → h264). VideoToolbox was the
same speed but produced **2.5× larger files** at equal quality target, and no longer
byte-matched. Trading 15% of a stage that was never the bottleneck for 2.5× storage is
a loss. Reject.

Remotion's default is `DEFAULT_OPENGL_RENDERER = null` — no `--use-gl` flag at all, so
Chromium picks. Do not assume that default is GPU-backed; it was slower than explicit
`angle-egl` here.

Wiring: `edit_decisions.bespoke.gl` (added to the schema alongside `scale`/`crf`/
`concurrency`), passed through by `video_compose.py`. Prefer an explicit per-project
field over a `remotion.config.ts`, which changes behaviour invisibly for every project
in the repo.

## Measured: Whisper transcription

`faster-whisper` runs on CTranslate2, which supports **CPU and CUDA only** — no Metal.
On Apple Silicon `device="cpu"` is therefore not a careless choice; there is no CUDA
device. Getting the GPU requires a different runtime.

**Check which runtime a tool uses before assuming the repo's device helper covers it.**
`docs/apple-silicon-mps.md` documents `get_torch_device()`, which routes **PyTorch**
tools to MPS — but CTranslate2, MLX, Chromium, and FFmpeg each have their own Metal
story and none of them go through that helper. A pipeline can look "GPU-enabled"
because one helper exists while the actual bottleneck runs on a runtime it never
touches. That doc now carries the runtime matrix.

13 files, 275.8s of audio, `medium` model:

| backend | time | realtime factor |
|---|---:|---:|
| faster-whisper int8 CPU | 143.1s | 1.93× |
| **mlx-whisper (Metal)** | **19.1s** | **14.43×** |

**7.5× faster.** But speed was never the question — the pipeline consumes *word
timestamps*, so the decisive test is what the downstream artifact looks like.

Running the full alignment step with MLX timestamps and diffing the final captions
against the shipped CPU captions:

- 1004 words, **text identical** (as designed — script words are canonical)
- **median offset 0 ms**, mean 14 ms
- **96% of words within ±50 ms**, 98% within ±100 ms
- worst case 1360 ms

## Trace the outliers before accepting or rejecting

The 1.36s outlier looked disqualifying. Tracing it found **two pre-existing weaknesses
in the alignment step, neither caused by Metal**:

1. **Number-dense passages.** The script spells numbers out so TTS reads them correctly
   in Vietnamese; ASR transcribes them back as digits. 81 script words → 66 ASR anchors,
   so 15 words are interpolated. Both backends interpolate; they just place their
   anchors slightly differently and the interpolated span slides.
2. **Repeated tokens.** The word "Write" appeared twice in one section. The sequence
   matcher anchored to a different occurrence under each backend.

Both failure modes exist on CPU too. Metal is not less accurate — it lands on the other
side of ambiguities the pipeline already had. That reframing is what makes adoption
safe, and it also surfaced a real bug to fix later, independent of GPU.

**Do not regenerate captions for already-rendered episodes.** The video has the old
captions baked in; regenerating desynchronises the sidecar file from the video with
nothing to flag it. Warned in the tool docstring.

## Keep a CPU fallback behind one flag

`captions.py --backend auto|mlx|cpu`, defaulting to `auto`: try Metal, fall back to CPU
on ImportError. Same output contract from both. A machine without MLX still works.

## What this exercise actually corrected

Timing the pipeline showed a full episode render takes **2.0–2.5 minutes**, not the
"15 minutes" I had been asserting all session — an estimate I never measured and then
reasoned from. Render was never the bottleneck; transcription was, at 146s versus 140s.

**Time your own pipeline before optimising it, and before telling anyone what it
costs.**

## Related

- `skills/meta/context-economics.md` — measuring before optimising, same lesson
- `projects/_sd-series/bench_gpu.py` — the video benchmark harness
