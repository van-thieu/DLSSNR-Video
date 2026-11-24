#!/usr/bin/env bash
# Sign the Firefox add-on with AMO so it can be installed permanently in Firefox Release
# (including the snap build). Unlisted signing is free and does not publish the add-on.
#
# Credentials come from AMO_API_KEY (JWT issuer) and AMO_API_SECRET (JWT secret), either exported
# or in ~/.config/dlssnr-video/amo-credentials as:
#   AMO_API_KEY="issuer:..."
#   AMO_API_SECRET="..."
# Get them at https://addons.mozilla.org/en-US/developers/addon/api/key/
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/firefox"
ART="$HERE/signed"
CRED="${AMO_CREDENTIALS:-$HOME/.config/dlssnr-video/amo-credentials}"

if { [[ -z "${AMO_API_KEY:-}" ]] || [[ -z "${AMO_API_SECRET:-}" ]]; } && [[ -r "$CRED" ]]; then
  # shellcheck disable=SC1090
  source "$CRED"
fi
if [[ -z "${AMO_API_KEY:-}" || -z "${AMO_API_SECRET:-}" ]]; then
  echo "AMO credentials not found. Export AMO_API_KEY / AMO_API_SECRET, or put them in" >&2
  echo "  $CRED" >&2
  echo "as two shell assignments (chmod 600). Create them at" >&2
  echo "  https://addons.mozilla.org/en-US/developers/addon/api/key/" >&2
  exit 1
fi

VERSION="$(python3 -c "import json;print(json.load(open('$SRC/manifest.json'))['version'])")"
echo "Signing DLSS5 Video Controls $VERSION (unlisted)..."

mkdir -p "$ART"
npx --yes web-ext sign \
  --source-dir "$SRC" \
  --artifacts-dir "$ART" \
  --channel unlisted \
  --api-key "$AMO_API_KEY" \
  --api-secret "$AMO_API_SECRET" \
  --no-input

SIGNED="$(find "$ART" -maxdepth 1 -name '*.xpi' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
if [[ -z "$SIGNED" ]]; then
  echo "Signing finished but no .xpi was produced in $ART" >&2
  exit 1
fi
echo
echo "Signed add-on: $SIGNED"
echo "Install it permanently: about:addons -> gear -> Install Add-on From File -> select that .xpi."
echo "After a code change, bump 'version' in $SRC/manifest.json (and manifest-firefox.json) before re-signing."
