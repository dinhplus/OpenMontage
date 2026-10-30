"""LTX-2.5 (22B) local video generation over HTTP.

Distinct from ``ltx_video_local``, which drives diffusers' ``LTXPipeline`` against
``Lightricks/LTX-2``. That loader cannot read LTX-2.5 checkpoints: 2.5 ships a
split per-component layout with a Gemma-4 text encoder bundled into a single
safetensors file, and is loaded by Lightricks' own ``ltx_pipelines`` package
rather than by diffusers.

So this tool talks to a small server that lives beside those weights and holds
the pipeline, the same shape ``comfyui_video`` uses for ComfyUI:

    LTX25_SERVER_URL   default http://127.0.0.1:8765

Start it with (in the LTX-2 checkout)::

    source .venv/bin/activate
    python local/serve.py --models-dir models/ltx-2.5 --port 8765

Unlike every other video tool in the registry, LTX-2.5 emits **synchronised
audio** with the video, so a clip from here already carries its own ambience and
diegetic sound. Do not layer a generated ambience bed over it by default.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
# A 145-frame 1024x576 clip measured ~12.5 min cold on an M3 Max (30-core GPU).
# Give the request generous headroom; the server caps the job itself.
DEFAULT_TIMEOUT_S = 3600


def _server_url() -> str:
    return os.environ.get("LTX25_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


def _request(path: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    url = f"{_server_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": body[:500]}


class LTX25Local(BaseTool):
    name = "ltx25_local"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "ltx"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.LOCAL_GPU

    dependencies = []
    install_instructions = (
        "Start the LTX-2.5 server in the LTX-2 checkout:\n"
        "  source .venv/bin/activate && python local/serve.py --models-dir models/ltx-2.5 --port 8765\n"
        "Then set LTX25_SERVER_URL if it is not http://127.0.0.1:8765.\n"
        "Needs ~66 GB of LTX-2.5 weights on disk and ~50 GB of unified memory while running."
    )
    fallback = "comfyui_video"
    fallback_tools = ["comfyui_video", "wan_video", "ltx_video_local", "image_selector"]
    agent_skills = ["ltx2", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "seed": True,
        "reference_image": True,
        "offline": True,
        # LTX-2.5 generates a synchronised audio track alongside the video.
        "native_audio": True,
        "local_gpu": True,
    }
    best_for = [
        "offline generation with synchronised diegetic audio in one pass",
        "chaining shots: feed the previous shot's last frame in as frame 0 for continuity",
        "Apple Silicon with large unified memory, where fp8 checkpoints do not run",
    ]
    not_good_for = [
        "fast iteration - a 6s clip takes minutes, not seconds",
        "machines with less than ~50 GB of free unified memory or VRAM",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {"type": "string", "enum": ["text_to_video", "image_to_video"], "default": "text_to_video"},
            "reference_image_path": {"type": "string", "description": "Local still used as conditioning."},
            "reference_frame_idx": {
                "type": "integer",
                "default": 0,
                "description": "Frame the reference is pinned to. 0 chains from a previous shot's last frame.",
            },
            "reference_strength": {"type": "number", "default": 1.0},
            "width": {"type": "integer", "default": 1024, "description": "Multiple of 64."},
            "height": {"type": "integer", "default": 576, "description": "Multiple of 64."},
            "num_frames": {"type": "integer", "default": 145, "description": "Must be 8k+1. 145 = 6.04s at 24fps."},
            "frame_rate": {"type": "number", "default": 24},
            "seed": {"type": "integer", "default": 10},
            "enhance_prompt": {"type": "boolean", "default": False},
            "output_path": {"type": "string"},
            "timeout_s": {"type": "number", "default": DEFAULT_TIMEOUT_S},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=4, ram_mb=50000, vram_mb=50000, disk_mb=68000, network_required=False)
    retry_policy = RetryPolicy(max_retries=0)  # a retry costs another full render
    idempotency_key_fields = ["prompt", "operation", "seed", "width", "height", "num_frames"]
    side_effects = ["writes video file to output_path"]
    user_visible_verification = [
        "Watch for motion coherence and hand/face artifacts",
        "Listen to the generated audio track - it is produced by the model, not added in post",
    ]

    def get_status(self) -> ToolStatus:
        try:
            code, body = _request("/health", timeout=5.0)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return ToolStatus.UNAVAILABLE
        if code != 200:
            return ToolStatus.DEGRADED if body.get("status") == "missing_models" else ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        """Scale the measured 1024x576/145-frame baseline by pixel-frames."""
        baseline_s = 750.0
        baseline_units = 1024 * 576 * 145
        units = (
            int(inputs.get("width", 1024))
            * int(inputs.get("height", 576))
            * int(inputs.get("num_frames", 145))
        )
        return baseline_s * max(units / baseline_units, 0.1)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        status = self.get_status()
        if status is ToolStatus.UNAVAILABLE:
            return ToolResult(
                success=False,
                error=f"LTX-2.5 server not reachable at {_server_url()}. " + self.install_instructions,
            )

        operation = inputs.get("operation", "text_to_video")
        ref = inputs.get("reference_image_path")
        if operation == "image_to_video" and not ref:
            return ToolResult(success=False, error="operation=image_to_video requires reference_image_path.")

        payload = {
            key: inputs[key]
            for key in (
                "prompt",
                "reference_image_path",
                "reference_frame_idx",
                "reference_strength",
                "width",
                "height",
                "num_frames",
                "frame_rate",
                "seed",
                "enhance_prompt",
                "output_path",
            )
            if key in inputs and inputs[key] is not None
        }
        if operation != "image_to_video":
            payload.pop("reference_image_path", None)
        payload["timeout_s"] = float(inputs.get("timeout_s", DEFAULT_TIMEOUT_S))

        start = time.time()
        try:
            code, body = _request("/generate", payload, timeout=payload["timeout_s"] + 60)
        except (urllib.error.URLError, OSError) as exc:
            return ToolResult(success=False, error=f"LTX-2.5 server request failed: {exc}")

        duration = time.time() - start
        if code != 200 or not body.get("output_path"):
            return ToolResult(
                success=False,
                error=body.get("error") or f"LTX-2.5 server returned HTTP {code}",
                data=body,
                duration_seconds=duration,
            )

        return ToolResult(
            success=True,
            data={
                "output_path": body["output_path"],
                "num_frames": body.get("num_frames"),
                "has_audio": body.get("has_audio", True),
                "timings": body.get("timings", {}),
                "job_id": body.get("job_id"),
            },
            artifacts=[body["output_path"]],
            cost_usd=0.0,
            duration_seconds=duration,
            seed=inputs.get("seed"),
            model="ltx-2.5-22b-distilled",
        )
