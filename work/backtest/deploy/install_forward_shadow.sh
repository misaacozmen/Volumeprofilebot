#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install_forward_shadow.sh /path/to/forward-shadow.zip"
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv unzip openssl

python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 11):
    raise SystemExit(
        f"signed release requires Python 3.11; found {sys.version.split()[0]}"
    )
PY

ARCHIVE="${1:?signed release archive is required}"
ARCHIVE="$(readlink -f "${ARCHIVE}")"
MANIFEST="${ARCHIVE%.zip}.manifest.json"
SIGNATURE="${ARCHIVE%.zip}.manifest.sig"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PUBLIC_KEY="${SCRIPT_DIR}/release-public-key.pem"
for required in "${ARCHIVE}" "${MANIFEST}" "${SIGNATURE}" "${PUBLIC_KEY}"; do
  [[ -f "${required}" ]] || { echo "Missing signed release component: ${required}" >&2; exit 1; }
done

SIGNATURE_BIN="$(mktemp)"
STAGING="/opt/forward-shadow.new.$$.${RANDOM}"
cleanup() {
  rm -f -- "${SIGNATURE_BIN}"
  if [[ -d "${STAGING}" && "${STAGING}" == /opt/forward-shadow.new.* ]]; then
    rm -rf -- "${STAGING}"
  fi
}
trap cleanup EXIT
base64 --decode "${SIGNATURE}" > "${SIGNATURE_BIN}"
openssl dgst -sha256 -verify "${PUBLIC_KEY}" -signature "${SIGNATURE_BIN}" "${MANIFEST}" >/dev/null
python3 - "${ARCHIVE}" "${MANIFEST}" <<'PY'
import hashlib, json, pathlib, sys
archive = pathlib.Path(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if manifest.get("schema_version") != 1 or manifest.get("profile") != "forward-shadow":
    raise SystemExit("release profile/schema mismatch")
if manifest.get("archive_file") != archive.name:
    raise SystemExit("release archive name mismatch")
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != manifest.get("archive_sha256"):
    raise SystemExit("release archive SHA-256 mismatch")
PY

id -u forwardshadow >/dev/null 2>&1 || useradd --system --home /var/lib/forward-shadow --shell /usr/sbin/nologin forwardshadow
install -d -o forwardshadow -g forwardshadow -m 0750 /var/lib/forward-shadow
install -d -o root -g forwardshadow -m 0750 /etc/forward-shadow

if [[ ! -f /etc/forward-shadow/capital.env ]]; then
  install -o root -g forwardshadow -m 0640 \
    "${SCRIPT_DIR}/../live_forward/capital.env.example" /etc/forward-shadow/capital.env
fi

[[ ! -e /opt/forward-shadow ]] || { echo "/opt/forward-shadow already exists; refusing overwrite" >&2; exit 1; }
mkdir -m 0755 "${STAGING}"
unzip -q "${ARCHIVE}" -d "${STAGING}"
[[ -f "${STAGING}/requirements-linux.lock" && -d "${STAGING}/wheelhouse-linux" ]] || {
  echo "Signed release lacks Linux locked wheelhouse" >&2; exit 1;
}
python3 -m venv "${STAGING}/.venv"
(
  cd -- "${STAGING}"
  .venv/bin/pip install --disable-pip-version-check --no-index --require-hashes \
    -r requirements-linux.lock
  .venv/bin/pip install --disable-pip-version-check --no-index --no-deps --no-build-isolation .
)
mv -- "${STAGING}" /opt/forward-shadow

install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/forward-shadow.service" /etc/systemd/system/forward-shadow.service
chown -R root:root /opt/forward-shadow
chmod -R go-w /opt/forward-shadow

/opt/forward-shadow/.venv/bin/python /opt/forward-shadow/scripts/run_capital_forward.py --output-root /var/lib/forward-shadow init
systemctl daemon-reload
systemctl enable --now forward-shadow.service
systemctl --no-pager --full status forward-shadow.service || true
