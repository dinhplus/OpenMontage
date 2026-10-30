"""VieNeu-TTS — local Vietnamese text-to-speech with instant voice cloning.

The only Vietnamese-native provider in the registry, and the only local TTS that
clones a voice from a short reference clip. 48 kHz, offline, no API key, no cost.
20 preset voices covering northern / central / southern accents in news,
storytelling and conversational styles.

Talks to a server that holds the model, the same shape ``ltx25_local`` uses::

    VIENEU_SERVER_URL   default http://127.0.0.1:8770

Start it with (in the VieNeu-TTS checkout)::

    source .venv/bin/activate
    python local/serve.py --port 8770

**Why a server and not a subprocess** (``piper_tts`` shells out to a binary
per call): measured on an M3 Max, VieNeu loads in **5.3 s** with its weights in
the page cache -- 26.4 s the first time ever, including the download -- and
synthesises a 5-second line in **0.6-0.9 s**. Load costs ~7x a synthesis, so a
40-line narration would spend ~3.5 minutes purely loading if each line spawned a
fresh process. Against a warm server the same work takes ~30 seconds.

Audio carries an inaudible watermark by default (``apply_watermark``), which is
the upstream project's choice; it can be turned off per call.
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

DEFAULT_SERVER_URL = "http://127.0.0.1:8770"
DEFAULT_VOICE = "Adam"
# Synthesis is sub-second per line; the only slow case is a cold server still
# loading the model on its first request.
REQUEST_TIMEOUT_S = 300


def _server_url() -> str:
    return os.environ.get("VIENEU_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


def _request(path: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    url = f"{_server_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
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


class VieNeuTTS(BaseTool):
    name = "vieneu_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "vieneu"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC  # sampled decoding: same text varies run to run
    runtime = ToolRuntime.LOCAL

    dependencies = []
    install_instructions = (
        "Clone and install VieNeu-TTS, then start its server:\n"
        "  git clone https://github.com/pnnbao97/VieNeu-TTS.git && cd VieNeu-TTS\n"
        "  uv sync\n"
        "  source .venv/bin/activate && python local/serve.py --port 8770\n"
        "Set VIENEU_SERVER_URL if it is not http://127.0.0.1:8770.\n"
        "Runs on CPU via ONNX Runtime (CoreML provider on Apple Silicon); no GPU required."
    )
    fallback = "google_tts"
    fallback_tools = ["google_tts", "elevenlabs_tts", "openai_tts", "piper_tts"]
    agent_skills = ["text-to-speech"]

    capabilities = ["text_to_speech", "voice_cloning", "offline_generation"]
    supports = {
        "voice_cloning": True,
        "multilingual": False,  # Vietnamese only
        "offline": True,
        "native_audio": True,
        "streaming": True,  # server exposes batch; upstream also has infer_stream
    }
    best_for = [
        "Vietnamese narration - the only Vietnamese-native provider in the registry",
        "cloning a specific Vietnamese voice from a few seconds of reference audio",
        "privacy-sensitive or zero-cost work: nothing leaves the machine",
        "regional accent control (northern / central / southern)",
    ]
    not_good_for = [
        "any language other than Vietnamese",
        "one-off calls against a cold server - the first request pays a ~5 s model load",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Vietnamese text. Emotion tags like [cười] are experimental."},
            "voice": {
                "type": "string",
                "default": DEFAULT_VOICE,
                "description": (
                    "Preset voice name, e.g. 'Mai Anh' (nữ Bắc, tin tức), 'Thái Sơn' (nam Nam, kể chuyện), "
                    "'Ngọc Trân' (nữ Trung), 'Phạm Tuyên' (nam Bắc). Call list_voices() for all 20."
                ),
            },
            "ref_audio": {
                "type": "string",
                "description": "Path to a reference clip to clone instead of using a preset. Overrides `voice`.",
            },
            "output_path": {"type": "string"},
            "temperature": {"type": "number", "default": 0.8},
            "top_k": {"type": "integer", "default": 25},
            "top_p": {"type": "number", "default": 0.95},
            "denoise": {"type": "boolean", "default": True},
            "apply_watermark": {
                "type": "boolean",
                "default": True,
                "description": "Upstream applies an inaudible watermark by default. Set false to disable.",
            },
        },
    }

    resource_profile = ResourceProfile(cpu_cores=4, ram_mb=4000, vram_mb=0, disk_mb=2000, network_required=False)
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=[])
    idempotency_key_fields = ["text", "voice", "ref_audio", "temperature"]
    side_effects = ["writes audio file to output_path"]
    user_visible_verification = [
        "Listen for correct Vietnamese tone marks (dấu) - wrong tones change the word",
        "Check the accent matches the intended region",
    ]

    def get_status(self) -> ToolStatus:
        try:
            code, _ = _request("/health", timeout=5.0)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE if code == 200 else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        """~0.14 s of compute per second of speech; ~14 Vietnamese chars per second."""
        chars = len(inputs.get("text", ""))
        return max(0.5, chars / 14.0 * 0.14)

    def list_voices(self) -> list[dict[str, Any]]:
        """Preset voices with gender / style, for an agent choosing a narrator."""
        try:
            code, body = _request("/voices", timeout=10.0)
        except (urllib.error.URLError, OSError):
            return []
        return body.get("voices", []) if code == 200 else []

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() is not ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error=f"VieNeu-TTS server not reachable at {_server_url()}. " + self.install_instructions,
            )

        text = (inputs.get("text") or "").strip()
        if not text:
            return ToolResult(success=False, error="'text' is required and must be non-empty.")

        payload = {
            key: inputs[key]
            for key in (
                "text",
                "voice",
                "ref_audio",
                "output_path",
                "temperature",
                "top_k",
                "top_p",
                "denoise",
                "apply_watermark",
            )
            if key in inputs and inputs[key] is not None
        }
        payload["text"] = text
        payload.setdefault("voice", DEFAULT_VOICE)

        start = time.time()
        try:
            code, body = _request("/speak", payload, timeout=REQUEST_TIMEOUT_S)
        except (urllib.error.URLError, OSError) as exc:
            return ToolResult(success=False, error=f"VieNeu-TTS request failed: {exc}")

        duration = round(time.time() - start, 2)
        if code != 200 or not body.get("output_path"):
            return ToolResult(
                success=False,
                error=body.get("error") or f"VieNeu-TTS server returned HTTP {code}",
                data=body,
                duration_seconds=duration,
            )

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "output": body["output_path"],
                "format": "wav",
                "sample_rate": body.get("sample_rate"),
                "audio_duration_s": body.get("duration_s"),
                "rtf": body.get("rtf"),
                "voice": body.get("voice"),
                "text_length": body.get("text_length"),
            },
            artifacts=[body["output_path"]],
            cost_usd=0.0,
            duration_seconds=duration,
            model="vieneu-v3-turbo",
        )
