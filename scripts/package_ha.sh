#!/bin/sh
# Build dist/junghome_ble.zip from custom_components/junghome_ble (the mesh library lives inside it): the files git
# tracks there, as they are in the working tree. Anything else under the folder (an export saved there, which holds
# every key; bytecode; an editor's backup) stays out of the zip.
set -e
cd "$(dirname "$0")/.."
rm -rf dist/junghome_ble && mkdir -p dist
git -C custom_components ls-files -z -- junghome_ble \
  | rsync -a --from0 --files-from=- --ignore-missing-args custom_components/ dist/
(cd dist && rm -f junghome_ble.zip && zip -qr junghome_ble.zip junghome_ble)
echo "built dist/junghome_ble.zip — unzip into <HA config>/custom_components/ and restart Home Assistant"
