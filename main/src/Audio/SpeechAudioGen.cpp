#include "SpeechAudioGen.h"
#include "Core/Application.h"
#include "VoicePreset.h"
#include <cmath>

DEFINE_LOG(SpeechAudioGen)

SpeechAudioGen::SpeechAudioGen(){
	reverb.setRoomSize(0.2f); // 0 = metalan/sjajan, 1 = taman/prigusen
	reverb.setDamping(0.8f); // duzina odjeka
	reverb.setWet(0.05f); // Koliko reverba cujes
}

void SpeechAudioGen::open(std::unique_ptr<AudioSource> resource){
	resource->open();
	this->resource = std::move(resource);
	compressor.reset();

	// Fork: voice preset effects, fixed for the whole utterance
	const Voice::Preset& preset = Voice::current();
	fx = preset.fx;
	reverb.setRoomSize(preset.reverbRoom);
	reverb.setWet(preset.reverbWet);
	ringPhase = 0.0f;
	crushCount = 0;
	crushHeld = 0;
}

void SpeechAudioGen::close(){
	resource->close();
	resource.reset();
}

size_t SpeechAudioGen::getData(uint8_t* buffer, size_t bytes){
	if(resource == nullptr){
		CMF_LOG(SpeechAudioGen, LogLevel::Debug, "early return, no resource");
		return 0;
	}

	size_t bytesTransferred = 0;
	while(bytesTransferred < bytes && (bool)*resource){
		auto i = resource->getData(buffer + bytesTransferred, bytes - bytesTransferred);
		if(i == 0) break;
		bytesTransferred += i;
	}
	const size_t sampleCount = bytesTransferred / sizeof(int16_t);
	auto* samples = reinterpret_cast<int16_t*>(buffer);
	compressor.process(samples, sampleCount);
	applyFx(samples, sampleCount);
	reverb.process(samples, sampleCount);
	return bytesTransferred;
}

void SpeechAudioGen::applyFx(int16_t* samples, size_t count){
	switch(fx){
		case Voice::Fx::None:
			return;

		case Voice::Fx::Ring: {
			// Ring modulator: multiplying by a low sine gives the classic metallic "robot" voice. Part dry so the
			// words stay clear.
			static constexpr float CarrierHz = 55.0f;
			static constexpr float Mix = 0.7f;
			static constexpr float Step = 2.0f * (float) M_PI * CarrierHz / (float) SampleRate;
			for(size_t i = 0; i < count; ++i){
				const float x = samples[i];
				const float y = x * (1.0f - Mix) + x * std::sin(ringPhase) * Mix;
				samples[i] = static_cast<int16_t>(y);
				ringPhase += Step;
				if(ringPhase > 2.0f * (float) M_PI) ringPhase -= 2.0f * (float) M_PI;
			}
			return;
		}

		case Voice::Fx::Crush: {
			// Bitcrusher: hold every sample for 3 (16 kHz -> ~5.3 kHz) and keep 6 bits - an old game console voice.
			static constexpr int Hold = 3;
			static constexpr int DropBits = 10;
			for(size_t i = 0; i < count; ++i){
				if(crushCount == 0){
					crushHeld = static_cast<int16_t>((samples[i] >> DropBits) << DropBits);
				}
				samples[i] = crushHeld;
				crushCount = (crushCount + 1) % Hold;
			}
			return;
		}
	}
}
