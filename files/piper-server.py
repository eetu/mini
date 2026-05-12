"""Streaming HTTP wrapper around piper-tts.

Replaces upstream's `piper.http_server` with one that emits the synthesised
audio as chunked WAV instead of buffering the whole utterance before
responding. Keeps the on-wire shape the same so existing clients (../chat)
don't have to change:

  GET  /voices         returns { "<slug>": <onnx.json contents>, ... }
  POST /               returns audio/wav (chunked)

Dropped vs upstream:
  GET  /all-voices     listed the full piper-voices catalog; we don't use it
  POST /download       on-demand voice install; we manage voices via IaC

Streaming WAV header trick: RIFF + data chunk sizes set to 0xFFFFFFFF
("infinite" / overflow) so we can write the header before knowing the
output length. Real-world WAV decoders (ffmpeg, gstreamer, sox, browser
MediaSource) handle this fine — they stream until EOF rather than honouring
the bogus size field. Clients that hard-bound on the header size won't be
happy; that's the trade-off for first-byte latency.
"""

import argparse
import json
import logging
import struct
from pathlib import Path

from flask import Flask, Response, abort, request
from piper import PiperVoice, SynthesisConfig

_LOGGER = logging.getLogger(__name__)


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

        def generate():
            sr = voice.config.sample_rate
            yield _wav_header(sample_rate=sr, channels=1, sample_width=2)
            for chunk in voice.synthesize(text, syn_config):
                # int16 LE PCM frames; AudioChunk exposes a precomputed bytes
                # view so we don't pay a conversion per chunk.
                yield chunk.audio_int16_bytes

        # Explicit Transfer-Encoding: chunked via the generator. Flask sets
        # it automatically when the response body is a generator.
        return Response(generate(), mimetype="audio/wav")

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
