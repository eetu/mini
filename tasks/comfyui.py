"""ComfyUI: opinionated diffusion stack on MPS.

This is a single-purpose deployment, not a generic ComfyUI install. The model
set, custom-node pin, and target subdirs are hardcoded here — the calling app
(../chat) talks to known fixed models. Three workflows ship under
files/comfyui-workflows/:
  - Flux.1 Kontext [dev] GGUF — edit-by-reference img2img
  - Flux.1 Fill [dev] GGUF    — masked inpaint / outpaint
  - Z-Image Turbo GGUF        — pure txt2img (replaces Ollama's flaky MLX
                                imagegen runner; see the model note below)
Only one diffusion model is resident per request — the workflow selects which.
Bumping COMFYUI["version"] re-fetches the source tarball, wipes
/Applications/ComfyUI/, and rebuilds the .venv from requirements.txt; weights
live outside the install dir and survive.

Why GGUF (not FP8):
  PyTorch's MPS backend has no kernel for Float8_e4m3fn, so loading Flux
  Kontext FP8 on Apple Silicon errors:
    `TypeError: Trying to convert Float8_e4m3fn to the MPS backend...`
  GGUF (a llama.cpp-derived quantization format) decodes to fp16 in pure
  PyTorch ops the MPS backend supports. Q6_K is the community-consensus sweet
  spot on 24 GB Macs — quality loss vs full-precision is "virtually
  imperceptible" per 2026 Apple-Silicon ComfyUI benchmarks, and the diffusion
  model drops from ~12 GB (FP8) to 9.17 GB. Workflows must use
  `UnetLoaderGGUF` / `DualCLIPLoaderGGUF` from the city96/ComfyUI-GGUF custom
  node (pinned below) instead of the stock `UNETLoader` / `DualCLIPLoader`.

Why Comfy-Org VAE mirror (not black-forest-labs):
  black-forest-labs/* repos require a HuggingFace token even for Apache-
  licensed files (FLUX.1-schnell ae.safetensors → 401 without auth). Comfy-Org
  hosts a re-packaged copy under Lumina_Image_2.0_Repackaged that downloads
  ungated. SHA256 differs from the official file (different safetensors header
  padding) but file size is identical and the file is the one Comfy-Org's own
  Kontext / Lumina workflows ship pointing at — works in practice. Switch to
  the official URL + HF_TOKEN if Kontext outputs ever show colour/decoding
  artifacts.

Memory:
  Flux Kontext Q6_K (~9 GB) + T5-XXL Q5_K_M (~3 GB) + CLIP-L (~0.25 GB) + VAE
  (~0.34 GB) + Hyper-FLUX 8-step LoRA (~1.3 GB when loaded) + TAESD-F1
  (~0.01 GB) = ~14 GB resident. Plus Gemma 26B (~18 GB) = 32 GB > 24 GB.
  The calling app coordinates eviction (per-request `keep_alive: 0` to ollama
  before invoking ComfyUI). Daemon keeps the checkpoint resident until exit.

Speed knobs:
  - PyTorch nightly is installed on top of ComfyUI's requirements.txt — its
    MPS kernels for attention/linear ops are 20-40% faster than stable
    torch on Apple Silicon. Trade-off: less stable, may need pinning if a
    nightly regresses.
  - Hyper-FLUX-8steps LoRA (ByteDance) lets workflows infer Kontext at
    8 steps + CFG=1 instead of 20 steps + CFG=2.5, ~5× faster. Workflow
    must wire a LoRA Loader before UnetLoaderGGUF; quality drop is visible
    on fine detail but acceptable for chat-style edits.
  - TAESD-F1 latent previews stream over /ws every step via
    `--preview-method taesd`. The decoder file at
    `vae_approx/taef1_decoder.pth` comes from the madebyollin/taesd GitHub
    repo — those .pth files are saved as flat-keyed `OrderedDict`s
    (`1.weight`, `3.conv.0.weight`, …) matching ComfyUI's preview loader.
    Note: the parallel HuggingFace `madebyollin/taef1` repo ships a
    diffusers-format AutoencoderTiny state_dict with `decoder.layers.X`
    prefixed keys that ComfyUI cannot load — use the GitHub URL, not HF.
    Result: small JPEG previews of the in-progress image, recognisable
    from step ~3 onward.
"""

import glob
import hashlib
import io
import os
import textwrap

from pyinfra.operations import files, server

from group_data.all import COMFYUI
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.comfyui"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/comfyui-run.sh"
INSTALL_PATH = "/Applications/ComfyUI"
VENV_PYTHON = f"{INSTALL_PATH}/.venv/bin/python"
VERSION_STAMP = f"{INSTALL_PATH}.version"
MODELS_PATH = "/Users/Shared/comfyui-models"
EXTRA_PATHS_YAML = f"{INSTALL_PATH}/extra_model_paths.yaml"
LOG_PATH = "/opt/homebrew/var/log/comfyui.log"

VERSION = COMFYUI["version"]
INTERNAL_PORT = COMFYUI["internal_port"]
TARBALL_URL = f"https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/{VERSION}.tar.gz"

# city96/ComfyUI-GGUF — needed for UnetLoaderGGUF + DualCLIPLoaderGGUF nodes
# that load Q6_K/Q5_K quantized weights via MPS-compatible fp16 dequant. The
# upstream repo doesn't cut tags or releases, so we pin to a commit SHA.
# Bump by editing this constant (verify the tree at the new SHA first):
#   https://github.com/city96/ComfyUI-GGUF/commits/main
GGUF_NODE_REPO = "https://github.com/city96/ComfyUI-GGUF"
GGUF_NODE_SHA = "6ea2651e7df66d7585f6ffee804b20e92fb38b8a"  # 2026-01-12
GGUF_NODE_DIR = f"{INSTALL_PATH}/custom_nodes/ComfyUI-GGUF"

# Pinned weight set for Flux Kontext img2img on MPS. Quants are GGUF (loaded
# via ComfyUI-GGUF) for the diffusion model + T5XXL — FP8 is unsupported on
# the PyTorch MPS backend. CLIP-L and the VAE stay as safetensors because
# they're small enough that quantizing them saves nothing meaningful.
# (subdir under MODELS_PATH, filename, direct download URL).
MODELS = (
    (
        "diffusion_models",
        "flux1-kontext-dev-Q6_K.gguf",
        "https://huggingface.co/QuantStack/FLUX.1-Kontext-dev-GGUF/resolve/main"
        "/flux1-kontext-dev-Q6_K.gguf",
    ),
    # Flux.1 Fill [dev] — masked inpaint / outpaint variant of Flux.1-dev.
    # Same envelope as Kontext (~9.86 GB Q6_K); only one diffusion model is
    # resident per request so disk-side coexistence is fine. Sourced from
    # YarvixPA (ungated); city96's parallel repo requires HF auth.
    (
        "diffusion_models",
        "flux1-fill-dev-Q6_K.gguf",
        "https://huggingface.co/YarvixPA/FLUX.1-Fill-dev-gguf/resolve/main"
        "/flux1-fill-dev-Q6_K.gguf",
    ),
    # Z-Image Turbo (Tongyi-MAI, 6B) — pure txt2img, distilled to ~8 steps.
    # Replaces Ollama's experimental MLX imagegen runner, which panics on
    # this exact model (ollama/ollama#16079, "index out of range" in the
    # qwen3 text encoder) — ComfyUI runs it cleanly. All-GGUF to match the
    # Flux stack: Q8_0 diffusion (~6.5 GB, unsloth dynamic quant) + a Qwen3-4B
    # text encoder GGUF (~3 GB). Peak resident ~11 GB vs the bf16 path's ~20 GB
    # (z_image_turbo_bf16 ~12 GB + qwen_3_4b ~8 GB), which is too tight on
    # 24 GB. Loaded via the same city96 UnetLoaderGGUF / CLIPLoaderGGUF nodes.
    # The VAE is the same Flux ae.safetensors already pulled below — reused,
    # not re-downloaded. See files/comfyui-workflows/z-image-turbo-txt2img.json.
    (
        "diffusion_models",
        "z-image-turbo-Q8_0.gguf",
        "https://huggingface.co/unsloth/Z-Image-Turbo-GGUF/resolve/main/z-image-turbo-Q8_0.gguf",
    ),
    # Qwen3-4B text encoder for Z-Image. ComfyUI core auto-detects this as
    # TEModel.QWEN3_4B and — because the CLIPLoader type is not flux/flux2 —
    # routes it to the z_image text encoder + ZImageTokenizer (comfy/sd.py).
    # No mmproj sidecar needed (txt2img path is text-only). UD-Q5_K_XL is the
    # unsloth-dynamic quant felipedpm packaged specifically for ComfyUI z-image.
    (
        "text_encoders",
        "Qwen3-4B-UD-Q5_K_XL.gguf",
        "https://huggingface.co/felipedpm/z-image-turbo-GGUF-confyui/resolve/main"
        "/models/text_encoders/Qwen3-4B-UD-Q5_K_XL.gguf",
    ),
    (
        "text_encoders",
        "t5-v1_1-xxl-encoder-Q5_K_M.gguf",
        "https://huggingface.co/city96/t5-v1_1-xxl-encoder-gguf/resolve/main"
        "/t5-v1_1-xxl-encoder-Q5_K_M.gguf",
    ),
    (
        "text_encoders",
        "clip_l.safetensors",
        "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors",
    ),
    (
        "vae",
        "ae.safetensors",
        "https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main"
        "/split_files/vae/ae.safetensors",
    ),
    # Hyper-FLUX-8steps LoRA — distilled adapter that lets workflows run
    # Kontext at ~8 steps + CFG=1 instead of 20 steps + CFG=2.5. Calling app
    # opts in via the LoRA Loader node in its workflow JSON.
    (
        "loras",
        "Hyper-FLUX.1-dev-8steps-lora.safetensors",
        "https://huggingface.co/ByteDance/Hyper-SD/resolve/main"
        "/Hyper-FLUX.1-dev-8steps-lora.safetensors",
    ),
    # TAESD-F1 preview decoder. Comes from madebyollin's taesd GitHub repo,
    # NOT the parallel `madebyollin/taef1` HF repo — the GitHub .pth files
    # are flat-keyed OrderedDicts matching ComfyUI's preview Sequential
    # loader, while the HF `diffusion_pytorch_model.safetensors` is a
    # diffusers AutoencoderTiny state_dict with `decoder.layers.X` prefixed
    # keys that ComfyUI can't load.
    (
        "vae_approx",
        "taef1_decoder.pth",
        "https://github.com/madebyollin/taesd/raw/main/taef1_decoder.pth",
    ),
)

# --- Models directory (root-owned, survives version bumps) ---
files.directory(
    name=f"Create {MODELS_PATH}",
    path=MODELS_PATH,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)
for _subdir in {entry[0] for entry in MODELS}:
    files.directory(
        name=f"Create {MODELS_PATH}/{_subdir}",
        path=f"{MODELS_PATH}/{_subdir}",
        user="root",
        group="wheel",
        mode="755",
        present=True,
    )

# --- ComfyUI source + venv install (pinned, version-stamped, idempotent) ---
# Bumping VERSION wipes INSTALL_PATH wholesale (incl. .venv) and rebuilds.
# uv handles its own python toolchain — no brew python dependency.
server.shell(
    name=f"Install ComfyUI {VERSION} + .venv",
    commands=[
        textwrap.dedent(f"""
        STAMP={VERSION_STAMP}
        if [ "$(cat "$STAMP" 2>/dev/null)" != "{VERSION}" ]; then
          TMP=$(mktemp -d)
          curl -fsSL -o "$TMP/comfyui.tar.gz" "{TARBALL_URL}"
          rm -rf {INSTALL_PATH}
          mkdir -p {INSTALL_PATH}
          tar -xzf "$TMP/comfyui.tar.gz" -C {INSTALL_PATH} --strip-components=1
          rm -rf "$TMP"
          /opt/homebrew/bin/uv venv --python 3.12 {INSTALL_PATH}/.venv
          /opt/homebrew/bin/uv pip install \\
            --python {VENV_PYTHON} \\
            -r {INSTALL_PATH}/requirements.txt
          # Override the stable torch ComfyUI's requirements.txt pulls with
          # the nightly build — its MPS attention/linear kernels are 20-40%
          # faster on Apple Silicon. torchaudio is intentionally omitted
          # (ComfyUI doesn't use it; reduces venv churn).
          /opt/homebrew/bin/uv pip install \\
            --python {VENV_PYTHON} \\
            --pre --upgrade torch torchvision \\
            --index-url https://download.pytorch.org/whl/nightly/cpu
          echo '{VERSION}' > "$STAMP"
        fi
        """).strip(),
    ],
)

# --- ComfyUI-GGUF custom node ---
# Cloned into custom_nodes/, pinned to GGUF_NODE_SHA. Lives inside
# INSTALL_PATH so a ComfyUI version bump (which wipes the install dir) also
# wipes the node and forces a fresh clone + dep reinstall — exactly what we
# want, since the venv is rebuilt too. The stamp at .deploy-stamp records the
# checked-out SHA so SHA-only bumps re-run without a ComfyUI version change.
server.shell(
    name=f"Install ComfyUI-GGUF custom node @ {GGUF_NODE_SHA[:8]}",
    commands=[
        textwrap.dedent(f"""
        STAMP={GGUF_NODE_DIR}/.deploy-stamp
        if [ "$(cat "$STAMP" 2>/dev/null)" != "{GGUF_NODE_SHA}" ]; then
          rm -rf {GGUF_NODE_DIR}
          mkdir -p {INSTALL_PATH}/custom_nodes
          git -c advice.detachedHead=false clone --quiet {GGUF_NODE_REPO} {GGUF_NODE_DIR}
          git -C {GGUF_NODE_DIR} checkout --quiet {GGUF_NODE_SHA}
          /opt/homebrew/bin/uv pip install \\
            --python {VENV_PYTHON} \\
            -r {GGUF_NODE_DIR}/requirements.txt
          echo '{GGUF_NODE_SHA}' > "$STAMP"
        fi
        """).strip(),
    ],
)

# --- Point ComfyUI at the external models tree ---
# extra_model_paths.yaml lives next to main.py and merges additional model
# directories on top of the in-tree defaults. Keeps weights out of the
# install dir so version bumps don't force redownload.
_extra_paths = textwrap.dedent(f"""
mini:
    base_path: {MODELS_PATH}/
    diffusion_models: diffusion_models/
    text_encoders: text_encoders/
    clip: text_encoders/
    vae: vae/
    vae_approx: vae_approx/
    checkpoints: checkpoints/
    loras: loras/
    controlnet: controlnet/
    clip_vision: clip_vision/
    upscale_models: upscale_models/
""").lstrip()

files.put(
    name="Write extra_model_paths.yaml",
    src=io.BytesIO(_extra_paths.encode()),
    dest=EXTRA_PATHS_YAML,
    user="root",
    group="wheel",
    mode="644",
)

# --- Wrapper script ---
# `cd` into INSTALL_PATH so ComfyUI picks up extra_model_paths.yaml and any
# relative defaults under custom_nodes/. --listen 127.0.0.1 keeps the
# upstream loopback-only; Caddy is the only thing that reaches us.
# --preview-method taesd streams in-progress latents through TAESD-F1 over
# /ws as small JPEGs every step. The decoder file is downloaded + converted
# in a separate block below (the upstream format needs key-stripping before
# ComfyUI's preview loader can parse it).
_wrapper = textwrap.dedent(f"""
#!/bin/sh
set -e
cd {INSTALL_PATH}
exec {VENV_PYTHON} main.py \\
    --listen 127.0.0.1 \\
    --port {INTERNAL_PORT} \\
    --preview-method taesd
""").lstrip()

files.put(
    name="Write comfyui wrapper",
    src=io.BytesIO(_wrapper.encode()),
    dest=WRAPPER_PATH,
    user="root",
    group="wheel",
    mode="755",
)

# --- LaunchDaemon plist ---
# HOME=/var/root keeps any python/pip/torch cache directories under root's
# home, not the deploy user's. KeepAlive=true respawns on crash.
_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{WRAPPER_PATH}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/var/root</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{LOG_PATH}</string>
</dict>
</plist>
"""

files.put(
    name="Write comfyui plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

# Hash plist + wrapper + extra-paths + version + port + custom-node SHA so
# any edit triggers a kickstart even when the plist text is byte-identical
# (which it usually is across version bumps). The custom-node SHA being in
# the hash matters: an SHA bump re-installs the node and we need the daemon
# to restart so the new node code loads.
_static_hash = hashlib.sha256(
    (_plist + _wrapper + _extra_paths + VERSION + str(INTERNAL_PORT) + GGUF_NODE_SHA).encode(),
).hexdigest()

server.shell(
    name="Bootstrap comfyui + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)

# --- Pull pinned model weights ---
# Each file is downloaded once and skipped on subsequent runs. We use curl's
# atomic temp-file pattern (.part + mv) so a deploy interrupted mid-download
# doesn't leave a half-written file that future runs would treat as present.
for _subdir, _filename, _url in MODELS:
    _dest = f"{MODELS_PATH}/{_subdir}/{_filename}"
    server.shell(
        name=f"Download {_subdir}/{_filename}",
        commands=[
            textwrap.dedent(f"""
            DEST="{_dest}"
            if [ ! -f "$DEST" ]; then
              mkdir -p "$(dirname "$DEST")"
              curl -fL --retry 3 --retry-delay 5 -o "$DEST.part" "{_url}"
              mv "$DEST.part" "$DEST"
            fi
            """).strip(),
        ],
    )

# --- Workflow registry ---
# Any .json in files/comfyui-workflows/ gets copied to ComfyUI's user workflow
# dir on the Mini. ComfyUI picks them up next time the daemon starts (UI scans
# at load); headless POSTs to /prompt don't need them present. Use this to
# version-control reusable workflow JSON alongside the IaC so the chat app's
# expected workflow shape can't drift from what's installed.
WORKFLOWS_LOCAL = "files/comfyui-workflows"
WORKFLOWS_REMOTE = f"{INSTALL_PATH}/user/default/workflows"

files.directory(
    name=f"Create {WORKFLOWS_REMOTE}",
    path=WORKFLOWS_REMOTE,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)

for _local_path in sorted(glob.glob(f"{WORKFLOWS_LOCAL}/*.json")):
    _basename = os.path.basename(_local_path)
    files.put(
        name=f"Sync workflow {_basename}",
        src=_local_path,
        dest=f"{WORKFLOWS_REMOTE}/{_basename}",
        user="root",
        group="wheel",
        mode="644",
    )
