#!/usr/bin/env bash
# CI helper: build the robot firmware inside the espressif/idf docker image and merge it into one image.
#
# Why the pinned component manager: CMF pulls espressif/esp32-camera in through a Kconfig-gated dependency
# (components/CMF/idf_component.yml). On a fresh build the component manager first runs before that component
# is downloaded, and the idf-component-manager bundled in espressif/idf:v5.5.3 (2.4.6) lets kconfgen drop the
# settings it doesn't know yet from sdkconfig, so they fall back to their Kconfig defaults. 2.5.2 (what the
# local Windows install uses) backs sdkconfig up across that pass. Without it the CI build left only ~27 KB of
# internal RAM free (69 KB for a local build) and the robot powered off when loading speech recognition.
# The same bug broke the controller build (ButterBotCtrl-Firmware-Public, scripts/ci_build.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
git config --global --add safe.directory "$PWD"

echo "component manager in the image: $(python -m pip show idf-component-manager | grep -i '^version')"
if [[ "${CI_CM_PIN:-1}" != "0" ]]; then
    python -m pip install --quiet "idf-component-manager==2.5.2"
fi
python -m pip show idf-component-manager | grep -i '^version'

idf.py build

# Guard: the build must not have rewritten the committed sdkconfig, and the camera component must have been
# configured from it (these are values that differ from esp32-camera's defaults).
if ! git diff --quiet -- sdkconfig; then
    echo "ERROR: the build changed sdkconfig (Kconfig values were lost):"
    git diff -U0 -- sdkconfig
    exit 1
fi
for want in 'CONFIG_CAMERA_TASK_STACK_SIZE 2048' 'CONFIG_CAMERA_PSRAM_DMA 1'; do
    if ! grep -q "^#define $want\$" build/config/sdkconfig.h; then
        echo "ERROR: build/config/sdkconfig.h lacks '$want'; the camera component was built with its defaults"
        exit 1
    fi
done

idf.py merge-bin -o ButterBot-merged.bin
