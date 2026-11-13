#ifndef BUTTERBOT_FIRMWARE_SPEECHAUDIOGEN_H
#define BUTTERBOT_FIRMWARE_SPEECHAUDIOGEN_H

#include "Services/Audio/AudioGenerator.h"
#include "Reverb.h"
#include "Compressor.h"
#include "VoicePreset.h"
/**
 * Interface between CMF Audio service and locally defined SpeechGen service.
 *
 * Basically just a passthrough of data from AudioSource, which is expected to be a SpeechAudioSource.
 */
class SpeechAudioGen : public AudioGenerator {
	using SampleType = int16_t;

public:
	SpeechAudioGen();

	void open(std::unique_ptr<AudioSource> resource) override;

	void close() override;

	size_t getData(uint8_t* buffer, size_t bytes) override;

private:
	std::unique_ptr<AudioSource> resource;
	int bytesRemaining = 0;
	Compressor compressor;
	Reverb reverb;

	// Fork: voice preset effect (see VoicePreset.h)
	static constexpr int SampleRate = 16000;
	void applyFx(int16_t* samples, size_t count);
	Voice::Fx fx = Voice::Fx::None;
	float ringPhase = 0.0f;
	int crushCount = 0;
	int16_t crushHeld = 0;
};


#endif //BUTTERBOT_FIRMWARE_SPEECHAUDIOGEN_H
