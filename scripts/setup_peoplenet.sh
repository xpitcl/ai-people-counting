#!/usr/bin/env bash
set -euo pipefail

CACHE_ROOT="${CACHE_ROOT:-${HOME}/.cache/ai-people-counting}"
MODEL_DIR="${CACHE_ROOT}/models/peoplenet"
ENGINE_DIR="${CACHE_ROOT}/engines"
MODEL_FILE="${MODEL_DIR}/resnet34_peoplenet_int8.onnx"
LABELS_FILE="${MODEL_DIR}/labels_peoplenet.txt"
MODEL_URL="https://api.ngc.nvidia.com/v2/models/nvidia/tao/peoplenet/versions/pruned_quantized_decrypted_v2.3.4/files/resnet34_peoplenet_int8.onnx"

mkdir -p "${MODEL_DIR}" "${ENGINE_DIR}"

download() {
  local url="$1"
  local output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL "${url}" -o "${output}"
  elif command -v wget >/dev/null 2>&1; then
    wget "${url}" -O "${output}"
  else
    echo "ERROR: instala curl o wget para descargar PeopleNet." >&2
    exit 1
  fi
}

if [[ ! -s "${MODEL_FILE}" ]]; then
  tmp_file="${MODEL_FILE}.tmp"
  echo "[INFO] Descargando PeopleNet ONNX podado desde NVIDIA NGC..."
  download "${MODEL_URL}" "${tmp_file}"
  mv "${tmp_file}" "${MODEL_FILE}"
else
  echo "[INFO] Modelo PeopleNet ya existe: ${MODEL_FILE}"
fi

cat > "${LABELS_FILE}" <<'EOF'
person
bag
face
EOF

echo "[OK] PeopleNet listo."
echo "[OK] Modelo: ${MODEL_FILE}"
echo "[OK] Labels: ${LABELS_FILE}"
echo "[OK] Engines: ${ENGINE_DIR}"
echo "[INFO] El engine TensorRT se generara en el primer arranque de DeepStream."
