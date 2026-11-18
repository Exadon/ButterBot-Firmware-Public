#include "Beeps.h"
#include <cmath>
#include <cstring>
#include <algorithm>
#include <memory>
#include <Services/Audio/Audio.h>
#include <Services/Audio/AudioGenerator.h>
#include "MemoryAudioSource.h"
#include "Services/Personal.h"
#include "Util/PSRAMAllocator.h"

namespace Beeps {

namespace {
	constexpr int Rate = 16000; // the speaker's I2S rate (HardwareConfiguration), mono 16-bit

	/** Plays raw PCM unchanged (the speech generator would put the voice effects on the beeps). */
	class PcmGenerator : public AudioGenerator {
	public:
		void open(std::unique_ptr<AudioSource> resource) override{
			source = std::move(resource);
			if(source) source->open();
		}

		void close() override{
			if(source) source->close();
			source.reset();
		}

		size_t getData(uint8_t* buffer, size_t bytes) override{
			return source ? source->getData(buffer, bytes) : 0;
		}

	private:
		std::unique_ptr<AudioSource> source;
	};

	PcmGenerator pcm;

	struct Note {
		float hz;
		int ms;            // 0 Hz = silence
		float level;       // 0..1 of full scale
		bool swell = false; // fade in and out over the whole note (a soft "breath") instead of 4 ms edges
	};

	/** The notes as PCM: a sine with a little second harmonic (a bit "robot"), 4 ms fades so nothing clicks. */
	PSRAMByteBuffer render(std::initializer_list<Note> notes){
		size_t total = 0;
		for(const Note& n : notes) total += (size_t) (Rate * n.ms / 1000);
		PSRAMByteBuffer out(total * sizeof(int16_t));
		auto* s = reinterpret_cast<int16_t*>(out.data());
		size_t at = 0;
		for(const Note& n : notes){
			const size_t len = (size_t) (Rate * n.ms / 1000);
			const size_t fade = std::min<size_t>(len / 2, Rate * 4 / 1000);
			for(size_t i = 0; i < len; ++i){
				float v = 0;
				if(n.hz > 0){
					const float t = (float) i / Rate;
					v = std::sin(2.0f * (float) M_PI * n.hz * t) * 0.8f + std::sin(4.0f * (float) M_PI * n.hz * t) * 0.2f;
					const float env = n.swell ? std::sin((float) M_PI * (float) i / (float) len)
					                          : std::min({ 1.0f, (float) i / (float) fade, (float) (len - i) / (float) fade });
					v *= env * n.level * 32767.0f;
				}
				s[at++] = (int16_t) v;
			}
		}
		return out;
	}

	void play(Audio* audio, PSRAMByteBuffer buf){
		if(audio == nullptr || !Personal::cueOn || audio->isPlaying() || buf.empty()) return;
		audio->play(&pcm, std::make_unique<MemoryAudioSource>(std::move(buf)));
	}
}

void heard(Audio* audio){
	play(audio, render({ { 660, 70, 0.30f }, { 0, 25, 0 }, { 990, 90, 0.30f } }));
}

void thinking(Audio* audio){
	// A soft low tone that swells in and out, every ~1.2 s (ListenState): a steady "breathing" pulse while it connects
	// and thinks. Low and quiet so it doesn't nag; two pitches alternate so it feels alive rather than like an alarm.
	static bool high = false;
	high = !high;
	play(audio, render({ { high ? 392.0f : 330.0f, 420, 0.10f, true } }));
}

}
