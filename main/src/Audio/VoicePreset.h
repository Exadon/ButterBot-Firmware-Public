#ifndef BUTTERBOT_FIRMWARE_VOICEPRESET_H
#define BUTTERBOT_FIRMWARE_VOICEPRESET_H

#include <atomic>
#include <cstdint>
#include <cstring>
#include <iterator>
#include <nvs.h>

/**
 * Fork: voice presets. The robot has one synthesis voice (Flite cmu_us_kal16 - the only one that fits in flash and
 * runs in real time on the ESP32-S3), so a preset reshapes that voice: pitch, pitch range (how sing-song it is),
 * speaking speed, plus an optional effect and reverb applied by SpeechAudioGen.
 * The choice is kept in NVS ("bbvoice"/"preset") and set over USB from Dimension Hopper ("voice <name>").
 */
namespace Voice {

enum class Fx : uint8_t { None, Ring, Crush };

struct Preset {
	const char* name;
	float pitch;      // int_f0_target_mean, Hz
	float range;      // int_f0_target_stddev, Hz
	float stretch;    // duration_stretch: >1 slower
	Fx fx;
	float reverbWet;
	float reverbRoom;
};

// Index 0 is the stock voice, exactly as CircuitMess shipped it.
inline constexpr Preset Presets[] = {
	{ "classic",   95.0f, 11.0f, 1.10f, Fx::None,  0.05f, 0.20f },
	{ "deep",      68.0f,  9.0f, 1.20f, Fx::None,  0.08f, 0.30f },
	{ "high",     150.0f, 18.0f, 1.00f, Fx::None,  0.05f, 0.20f },
	{ "sassy",    115.0f, 26.0f, 0.92f, Fx::None,  0.05f, 0.20f },
	{ "deadpan",   85.0f,  2.0f, 1.30f, Fx::None,  0.05f, 0.20f },
	{ "robot",    100.0f,  1.0f, 1.10f, Fx::Ring,  0.08f, 0.20f },
	{ "retro",    110.0f,  8.0f, 1.05f, Fx::Crush, 0.00f, 0.20f },
	{ "announcer", 82.0f, 14.0f, 1.15f, Fx::None,  0.28f, 0.75f },
};
inline constexpr uint8_t Count = std::size(Presets);
inline constexpr float StockStretch = 1.10f;

inline std::atomic<uint8_t> currentIndex{ 0 };

inline const Preset& current(){
	const uint8_t i = currentIndex.load();
	return Presets[i < Count ? i : 0];
}

// Name or number -> index, or -1.
inline int find(const char* nameOrIndex){
	for(uint8_t i = 0; i < Count; ++i){
		if(strcmp(nameOrIndex, Presets[i].name) == 0) return i;
	}
	if(nameOrIndex[0] >= '0' && nameOrIndex[0] <= '9' && nameOrIndex[1] == '\0'){
		const int i = nameOrIndex[0] - '0';
		return i < Count ? i : -1;
	}
	return -1;
}

inline void load(){
	nvs_handle_t h;
	if(nvs_open("bbvoice", NVS_READONLY, &h) != ESP_OK) return;
	uint8_t v = 0;
	if(nvs_get_u8(h, "preset", &v) == ESP_OK && v < Count) currentIndex = v;
	nvs_close(h);
}

inline void set(uint8_t i){
	if(i >= Count) return;
	currentIndex = i;
	nvs_handle_t h;
	if(nvs_open("bbvoice", NVS_READWRITE, &h) != ESP_OK) return;
	nvs_set_u8(h, "preset", i);
	nvs_commit(h);
	nvs_close(h);
}

}

#endif //BUTTERBOT_FIRMWARE_VOICEPRESET_H
