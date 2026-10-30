# Apple Silicon (MPS) Support

OpenMontage supports Apple Silicon Macs (M1/M2/M3/M4/M5) via PyTorch's
Metal Performance Shaders (MPS) backend. Local GPU tools — video generation,
upscaling, and face restoration — automatically detect and use MPS when
available.

## Requirements

- macOS 12.3 (Monterey) or later
- Apple Silicon Mac (M-series chip)
- Python 3.10+

## Quick Setup

```bash
# Enable local generation
export VIDEO_GEN_LOCAL_ENABLED=true

# Install dependencies — MPS support is included in the default torch wheel
uv pip install diffusers transformers accelerate torch pillow requests

# For upscaling and face restoration
uv pip install realesrgan gfpgan
```

No special CUDA build or separate MPS package is needed — `uv pip install torch`
on macOS automatically includes MPS support.

## How It Works

The `get_torch_device()` helper in `tools/video/_shared.py` detects the best
available device:

1. **CUDA** (NVIDIA GPU) — used when available; fastest for diffusion models
2. **MPS** (Apple Silicon Metal) — used on M-series Macs; good performance
3. **CPU** — fallback, always available but significantly slower

Device selection is automatic. All local GPU tools (`upscale`, `face_restore`,
`ltx_video_local`, `wan_video_local`, etc.) route through this helper.

## Not Everything Goes Through PyTorch

`get_torch_device()` only helps tools built on **PyTorch**. Several production tools use
other runtimes, and each has a different Metal story. Getting this wrong looks like
"the GPU isn't being used" when in fact the framework has no Metal backend at all.

| runtime | used by | Metal support |
|---|---|---|
| PyTorch | `upscale`, `face_restore`, local video gen | **yes** — via `get_torch_device()` |
| CTranslate2 | `faster-whisper` | **no** — CPU and CUDA only |
| MLX | `mlx-whisper` | **yes** — Metal native |
| Chromium | Remotion renders | via `--gl` flag, not PyTorch |
| FFmpeg | encode/master | via VideoToolbox, not PyTorch |

Measured on an M3 Max, transcribing 275.8s of audio with the `medium` model:

- `faster-whisper` (CTranslate2, int8, CPU) — **143.1s**, 1.93× realtime
- `mlx-whisper` (MLX, Metal) — **19.1s**, 14.43× realtime

**7.5× faster**, and the caption timings that come out differ by a median of 0 ms after
the alignment step. On Apple Silicon there is no CUDA device, so `device="cpu"` in
faster-whisper is not a careless choice — it is the only option that runtime offers.
Getting the GPU requires switching runtimes, not switching flags.

For Remotion, `--gl=angle-egl` measured 16% faster than the default with SSIM exactly
1.000000 (pixel-identical). VideoToolbox hardware encoding was the same speed but
produced 2.5× larger files — rejected. See `skills/meta/gpu-acceleration.md` for the
benchmark method: **always measure wall time and output fidelity together.**

## Known Limitations

- **VRAM**: Apple Silicon uses unified memory. Models that require >16 GB VRAM
  may not fit on 16 GB Macs. Check the tool's `resource_profile.vram_mb`.
- **bfloat16**: Not supported on MPS. The pipeline automatically uses float16
  on MPS and float32 on CPU.
- **CPU offloading**: `enable_model_cpu_offload()` is CUDA-only. On MPS, the
  pipeline falls back to direct device placement.
- **Half-precision in Real-ESRGAN**: fp16 can produce NaN artifacts on MPS, so
  upscaling automatically uses fp32 on non-CUDA devices.

## Verifying MPS Is Active

```python
from tools.video._shared import get_torch_device
print(get_torch_device())  # Should print "mps" on Apple Silicon
```

If this prints `"cpu"` on an Apple Silicon Mac, verify:
- macOS version is 12.3+
- PyTorch is installed (`uv pip install torch`)
- You're running native ARM Python (not Rosetta x86)
