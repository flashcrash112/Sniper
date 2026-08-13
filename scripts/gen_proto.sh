#!/usr/bin/env bash
# Generate the Yellowstone geyser gRPC stubs used by the `yellowstone` backend.
#
# The .proto files are fetched rather than vendored because they have to match
# the plugin version your provider runs. If your provider publishes a pinned
# version, set YELLOWSTONE_REF to that tag.
#
#   ./scripts/gen_proto.sh                    # track master
#   YELLOWSTONE_REF=v6.1.0 ./scripts/gen_proto.sh
set -euo pipefail

REF="${YELLOWSTONE_REF:-master}"
BASE_URL="https://raw.githubusercontent.com/rpcpool/yellowstone-grpc/${REF}/yellowstone-grpc-proto/proto"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT}/sniper/listener/proto"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

if ! python3 -c "import grpc_tools" 2>/dev/null; then
  echo "grpcio-tools is required. Install it with:" >&2
  echo "    pip install 'grpcio-tools>=1.60'" >&2
  exit 1
fi

echo "Fetching Yellowstone protos (ref: ${REF})..."
for proto in geyser.proto solana-storage.proto; do
  curl -fsSL "${BASE_URL}/${proto}" -o "${WORK_DIR}/${proto}"
done

mkdir -p "${OUT_DIR}"
touch "${OUT_DIR}/__init__.py"

echo "Generating stubs into ${OUT_DIR}..."
python3 -m grpc_tools.protoc \
  --proto_path="${WORK_DIR}" \
  --python_out="${OUT_DIR}" \
  --grpc_python_out="${OUT_DIR}" \
  "${WORK_DIR}/geyser.proto" "${WORK_DIR}/solana-storage.proto"

# protoc emits absolute top-level imports ("import geyser_pb2"), which do not
# resolve inside a package. Rewrite them to relative imports.
python3 - "${OUT_DIR}" <<'PY'
import pathlib
import re
import sys

out = pathlib.Path(sys.argv[1])
pattern = re.compile(r"^import (\w+_pb2) as (\w+)$", re.MULTILINE)
for path in out.glob("*_pb2*.py"):
    text = path.read_text()
    patched = pattern.sub(r"from . import \1 as \2", text)
    if patched != text:
        path.write_text(patched)
        print(f"  patched imports in {path.name}")
PY

echo "Done. The 'yellowstone' backend is now available."
