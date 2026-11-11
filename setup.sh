#!/bin/bash
# Rebuilds the toolchain entirely inside this folder (uv, Python 3.12, venv, fonts, voice model).
set -euo pipefail
cd "$(dirname "$0")"
export UV_PYTHON_INSTALL_DIR="$PWD/.tools/python" UV_CACHE_DIR="$PWD/.cache/uv" HF_HOME="$PWD/.cache/hf"
mkdir -p .tools/bin brand/fonts data work out secrets
[ -x .tools/bin/uv ] || curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$PWD/.tools/bin" sh
[ -d .venv ] || .tools/bin/uv venv --python 3.12 .venv
.tools/bin/uv pip install --python .venv/bin/python "kokoro>=0.9.4" soundfile pillow imageio-ffmpeg \
  google-api-python-client google-auth-oauthlib trafilatura \
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
G=https://github.com/google/fonts/raw/main/ofl
[ -f brand/fonts/Anton-Regular.ttf ] || curl -sfLo brand/fonts/Anton-Regular.ttf $G/anton/Anton-Regular.ttf
[ -f brand/fonts/Inter.ttf ] || curl -sfLo brand/fonts/Inter.ttf "$G/inter/Inter%5Bopsz,wght%5D.ttf"
.tools/bin/uv cache clean
./rw selftest
