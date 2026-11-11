#ifndef BUTTERBOT_FIRMWARE_SOUNDEFFECTS_H
#define BUTTERBOT_FIRMWARE_SOUNDEFFECTS_H

/**
 * Short SFX clips in SPIFFS, played at random for unmute and for the RC "sound" button.
 * Previously copy-pasted in main.cpp and RCState.cpp. ListenState keeps its own grouped
 * table because it also needs each clip's duration.
 */
namespace SoundEffects {

inline constexpr const char* Random[] = {
	"/spiffs/listen/lasers1/phaseJump1.aac",
	"/spiffs/listen/lasers1/phaseJump2.aac",
	"/spiffs/listen/lasers1/phaseJump3.aac",
	"/spiffs/listen/lasers1/phaseJump4.aac",
	"/spiffs/listen/lasers1/phaseJump5.aac",
	"/spiffs/listen/lasers1/phaserDown1.aac",
	"/spiffs/listen/lasers1/phaserDown2.aac",
	"/spiffs/listen/lasers1/phaserDown3.aac",
	"/spiffs/listen/lasers2/laser1.aac",
	"/spiffs/listen/lasers2/laser2.aac",
	"/spiffs/listen/lasers2/laser3.aac",
	"/spiffs/listen/lasers2/laser4.aac",
	"/spiffs/listen/lasers2/laser5.aac",
	"/spiffs/listen/lasers2/laser6.aac",
	"/spiffs/listen/lasers2/laser7.aac",
	"/spiffs/listen/lasers2/laser8.aac",
	"/spiffs/listen/lasers2/laser9.aac",
	"/spiffs/listen/robot/robot_1.aac",
	"/spiffs/listen/robot/robot_2.aac",
	"/spiffs/listen/robot/robot_3.aac",
	"/spiffs/listen/robot/robot_4.aac",
};

}

#endif //BUTTERBOT_FIRMWARE_SOUNDEFFECTS_H
