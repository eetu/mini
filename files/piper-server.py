"""Streaming HTTP wrapper around piper-tts.

Replaces upstream's `piper.http_server` with one that emits the synthesised
audio as a chunked Transfer-Encoding response instead of buffering the whole
utterance before responding. First byte ships as soon as piper's first
AudioChunk arrives. Two output formats:

  POST /                  → audio/wav   (default; 16-bit PCM at the voice's
                            native sample rate, ~352 kbps for 22050 Hz mono)
  POST /?format=opus      → audio/ogg   (libopus VBR ~32 kbps voice profile,
                            ~10x bandwidth reduction, clean Browser MSE
                            playback). Requires `ffmpeg` on PATH (Brewfile).

Endpoint shapes preserved from upstream so ../chat doesn't have to change:

  GET  /voices            { "<slug>": <onnx.json contents>, ... }
  POST /                  body fields: text, voice, speaker(_id),
                          length_scale, noise_scale, noise_w_scale

Dropped vs upstream:
  GET  /all-voices        listed the full piper-voices catalog; not needed
  POST /download          on-demand voice install; we manage voices via IaC

Streaming WAV header trick: RIFF + data chunk sizes set to 0xFFFFFFFF
("infinite" / overflow) so we can write the header before knowing the
output length. Real-world WAV decoders (ffmpeg, gstreamer, sox, browser
MediaSource) handle this fine — they stream until EOF rather than honouring
the bogus size field. Opus/Ogg has no equivalent issue: ogg pages are
self-delimiting, so MSE consumes it cleanly.
"""

import argparse
import contextlib
import json
import logging
import shutil
import struct
import subprocess
import threading
from pathlib import Path

from flask import Flask, Response, abort, request
from piper import PiperVoice, SynthesisConfig

_LOGGER = logging.getLogger(__name__)

# Opus VBR target. 32k @ -application voip is near-transparent for speech;
# 64k for music. Bump if you find sibilants getting smeared.
OPUS_BITRATE = "32k"
# How much we read from ffmpeg's stdout per yield. 8 KiB keeps the
# perceptual latency well under 100 ms at 32 kbps.
OPUS_READ_CHUNK = 8192


def _wav_header(sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """RIFF header with overflow size fields for streaming responses."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    bits_per_sample = sample_width * 8
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)  # file size minus 8 (overflow)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)  # fmt chunk size
        + struct.pack("<H", 1)  # PCM format tag
        + struct.pack("<H", channels)
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", byte_rate)
        + struct.pack("<H", block_align)
        + struct.pack("<H", bits_per_sample)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)  # data chunk size (overflow)
    )


def _ffmpeg_path() -> str:
    """Resolve ffmpeg. Brew puts it at /opt/homebrew/bin on Apple Silicon."""
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg not found on PATH; install via Brewfile")
    return found


def _stream_wav(voice, text, syn_config):
    """Generator yielding overflow-header WAV + raw PCM frames."""
    yield _wav_header(sample_rate=voice.config.sample_rate, channels=1, sample_width=2)
    for chunk in voice.synthesize(text, syn_config):
        # int16 LE PCM frames; AudioChunk exposes a precomputed bytes view
        # so we don't pay a conversion per chunk.
        yield chunk.audio_int16_bytes


def _stream_opus(voice, text, syn_config):
    """Generator yielding Ogg/Opus by piping raw PCM through ffmpeg.

    ffmpeg reads s16le from stdin at the voice's native sample rate and
    writes ogg-packaged Opus to stdout. A background thread feeds PCM as
    piper yields it; we read the encoded output in this generator and pass
    it straight back to the client. On client disconnect Flask drops the
    response, the generator gets GC'd, and the `finally` block tears down
    the subprocess + feeder thread.
    """
    sr = voice.config.sample_rate
    proc = subprocess.Popen(
        [
            _ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            # Input: raw PCM matching piper's native output.
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "1",
            "-i",
            "-",
            # Output: VBR Opus tuned for voice, in an Ogg container.
            "-c:a",
            "libopus",
            "-b:a",
            OPUS_BITRATE,
            "-vbr",
            "on",
            "-application",
            "voip",
            "-f",
            "ogg",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
    )

    def feed():
        try:
            for chunk in voice.synthesize(text, syn_config):
                proc.stdin.write(chunk.audio_int16_bytes)
        except (BrokenPipeError, ValueError):
            # Client disconnected or ffmpeg was terminated mid-write.
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    try:
        while True:
            data = proc.stdout.read(OPUS_READ_CHUNK)
            if not data:
                break
            yield data
    finally:
        # Reap even if the client bailed mid-stream.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        feeder.join(timeout=1)


def create_app(data_dir: Path, default_model: str) -> Flask:
    app = Flask(__name__)
    loaded: dict[str, PiperVoice] = {}

    def load_voice(model_id: str):
        if model_id in loaded:
            return loaded[model_id]
        path = data_dir / f"{model_id}.onnx"
        if not path.exists():
            return None
        _LOGGER.info("Loading voice %s", model_id)
        loaded[model_id] = PiperVoice.load(path)
        return loaded[model_id]

    if load_voice(default_model) is None:
        raise RuntimeError(f"default voice {default_model!r} not found under {data_dir}")

    @app.route("/voices", methods=["GET"])
    def list_voices():
        # Same JSON shape as upstream: dict keyed by slug, value is the
        # parsed onnx.json. ../chat reads this for the language picker.
        out: dict[str, dict] = {}
        for cfg_path in sorted(data_dir.glob("*.onnx.json")):
            slug = cfg_path.name.removesuffix(".onnx.json")
            with open(cfg_path, encoding="utf-8") as f:
                out[slug] = json.load(f)
        return out

    @app.route("/", methods=["POST"])
    def synthesize():
        body = request.get_json(force=True, silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            abort(400, "text required")

        model_id = body.get("voice") or default_model
        voice = load_voice(model_id)
        if voice is None:
            abort(400, f"voice not found: {model_id}")

        # Speaker handling mirrors upstream so multi-speaker voices keep
        # working unchanged.
        speaker_id = body.get("speaker_id")
        if (voice.config.num_speakers > 1) and (speaker_id is None):
            speaker = body.get("speaker")
            if speaker:
                speaker_id = voice.config.speaker_id_map.get(speaker)
            if speaker_id is None:
                speaker_id = 0

        syn_config = SynthesisConfig(
            speaker_id=speaker_id,
            length_scale=float(body.get("length_scale", voice.config.length_scale)),
            noise_scale=float(body.get("noise_scale", voice.config.noise_scale)),
            noise_w_scale=float(body.get("noise_w_scale", voice.config.noise_w_scale)),
        )

        fmt = (request.args.get("format") or body.get("format") or "wav").lower()
        if fmt in ("opus", "ogg"):
            return Response(
                _stream_opus(voice, text, syn_config),
                mimetype="audio/ogg",
            )
        return Response(
            _stream_wav(voice, text, syn_config),
            mimetype="audio/wav",
        )

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8193)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--default-model", required=True)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    app = create_app(args.data_dir, args.default_model)
    # threaded=True so /voices isn't blocked while a long /  is streaming.
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
