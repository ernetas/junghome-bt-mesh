#!/bin/sh
# Build dist/junghome_ble.zip from custom_components/junghome_ble (the mesh library lives inside it).
set -e
cd "$(dirname "$0")/.."
rm -rf dist/junghome_ble && mkdir -p dist/junghome_ble
rsync -a --exclude '__pycache__' custom_components/junghome_ble/ dist/junghome_ble/
(cd dist && rm -f junghome_ble.zip && zip -qr junghome_ble.zip junghome_ble)
echo "built dist/junghome_ble.zip — unzip into <HA config>/custom_components/ and restart Home Assistant"
