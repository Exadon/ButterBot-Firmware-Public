# ButterBot Firmware

> **This is a community fork** of [CircuitMess/ButterBot-Firmware-Public](https://github.com/CircuitMess/ButterBot-Firmware-Public).
> It is compatible with the stock controller firmware.

## What's different in this fork

- **Sturdier at runtime:** an I2C glitch on the motor board link is logged instead of rebooting the robot, and routines skip themselves instead of crashing when a service is missing.
- **Settings survive firmware updates.** They used to reset to defaults whenever the settings layout changed.
- **Bug fixes:** an uninitialized power-button state, volume jumping after mute/unmute, skewed odds for random idle behaviours, and a few smaller issues in listening mode.
- **[Dimension Hopper](tools/updater):** a one-click Windows updater for the robot *and* the controller. It flashes GitHub releases, local images or `idf.py` build folders, with automatic backups and wrong-board protection.

## Installing a release

Every `v*` tag builds a single merged image on GitHub Actions and attaches it to the
[releases page](../../releases). The easiest way to install it is Dimension Hopper: choose
*Latest GitHub release* and enter this repository's `owner/name`.

## Building

To build the ButterBot firmware, you'll need the ESP-IDF. You can find the getting started
guide [here](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/get-started/).
The production firmware is built using IDF version 5.5.3.

All required components are contained in the repository, and managed components (esp-sr,
esp-dl, esp-tflite-micro, face detection and recognition models, etc.) are fetched
automatically by the IDF component manager during the build.

In the root directory of the project:

**To build the firmware** run ```idf.py build```

**To upload the firmware to the device** run ```idf.py -p <PORT> flash```. Replace `<PORT>` with
the port the robot is attached to, for ex. ```COM6``` or ```/dev/ttyACM0```.

Flashing also uploads the data partitions: the SPIFFS filesystem image built from
[spiffs](spiffs), the object detection model from [models](models), and the speech
recognition and face detection / recognition models provided by the managed components.

## Restoring the stock firmware

To restore the stock firmware, you can download the prebuilt binary on
the [releases page](https://github.com/CircuitMess/ButterBot-Firmware-Public/releases) of
this repository and flash it manually using esptool:

```shell
esptool -c esp32s3 -b 921600 -p <PORT> write_flash 0 ButterBot-Firmware.bin
```

Alternatively, you can also do so using [CircuitBlocks](https://code.circuitmess.com/) by
logging in, clicking the "Restore Firmware" button in the top-right corner, and following the
on-screen instructions.
