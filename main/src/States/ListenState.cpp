#include "ListenState.h"
#include "BBStateMachine.h"
#include "Audio/AudioFrontend.h"
#include "Audio/SpeechAudioGen.h"
#include "Audio/SpeechAudioSource.h"
#include "Audio/SpeechGen.h"
#include "Audio/VoicePreset.h"
#include <Core/Application.h>
#include <Statics/ApplicationStatics.h>
#include <Phrases.h>
#include <Services/Audio/Audio.h>
#include <Services/Audio/AACAudioGenerator.h>
#include "Audio/MemoryAudioSource.h"
#include "Util/PSRAMAllocator.h"
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <span>
#include <vector>
#include <Util/ServiceLocator.h>
#include "BBData.h"
#include "Enums.h"
#include "IdleState.h"
#include "RCState.h"
#include "ScenarioState.h"
#include "Services/Com.h"
#include "Services/ScenarioRoutineService.h"
#include "Services/IRStorage.h"
#include "Routines/IRActionRoutine.h"
#include "Services/HomeWiFi.h"
#include "Services/BrainLink.h"
#include "Audio/VoicePreset.h"
#include "Util/MemMark.h"
#include <atomic>
#include <freertos/task.h>
#include "ScenarioRoutineMappings.h"

DEFINE_LOG(ListenState)

static void playPhraseBlocking(const Application* app, Phrase phrase){
    if(app == nullptr){
        return;
    }

    Audio* audio = app->getService<Audio>();
    if(audio == nullptr || !ServiceLocator::SpeechAudioGenInstance || !ServiceLocator::SpeechGenInstance){
        return;
    }

    const int16_t id = Phrases::get(phrase);
    if(id < 0){
        return;
    }

    const std::string text = Phrases::map(phrase, id);
    if(text.empty()){
        return;
    }

    auto source = std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text);
    audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::move(source));
    audio->waitEnd(portMAX_DELAY);
}

static const char* TAG = "ListenState";

// TEMPORARY instrumentation (perf/listen-timing): where does the time go between the button press
// and the robot actually listening, and how much PSRAM does the recogniser take?
// Logged at Warning level (the firmware filters everything below that) under one tag so it's easy to grep.
#include <esp_heap_caps.h>
#include <esp_timer.h>
namespace ListenTiming {
    static int64_t t0 = 0;
    static void mark(const char* what){
        const int64_t now = esp_timer_get_time();
        if(t0 == 0) t0 = now;
        ESP_LOGW("LISTEN_T", "%-22s +%5lld ms | PSRAM free %6u KB, largest %6u KB | internal free %4u KB",
                 what, (now - t0) / 1000,
                 (unsigned) (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024),
                 (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM) / 1024),
                 (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
    }
}

namespace {
    // Fork: the prompt ("Yes?", "I'm listening"...) is the cue to speak, so it must not end before the recogniser
    // is ready. Loading the model and building the command list takes ~3.0-3.1 s; with the old 2500 ms the prompt
    // ended ~0.5 s early and the start of whatever the user said straight after it was never heard.
    constexpr uint32_t IntroWindowMs = 3200;

    struct SoundEffect { const char* path; uint16_t durationMs; };
    struct StartPrompt { const char* text; uint16_t durationMs; };

    constexpr StartPrompt StartPrompts[] = {
        { "Yes?",                  753 },
        { "I'm listening",         978 },
        { "How can I help?",      1303 },
        { "Another task?",        1010 },
        { "How may I assist?",    1417 },
        { "You have my attention", 1531 },
        { "Awaiting input",       1144 },
        { "State your request",   1511 },
    };

    constexpr SoundEffect Lasers1[] = {
        { "/spiffs/listen/lasers1/phaseJump1.aac",  384 },
        { "/spiffs/listen/lasers1/phaseJump2.aac",  384 },
        { "/spiffs/listen/lasers1/phaseJump3.aac",  320 },
        { "/spiffs/listen/lasers1/phaseJump4.aac",  256 },
        { "/spiffs/listen/lasers1/phaseJump5.aac",  384 },
        { "/spiffs/listen/lasers1/phaserDown1.aac", 384 },
        { "/spiffs/listen/lasers1/phaserDown2.aac", 320 },
        { "/spiffs/listen/lasers1/phaserDown3.aac", 384 },
    };

    constexpr SoundEffect Lasers2[] = {
        { "/spiffs/listen/lasers2/laser1.aac", 384 },
        { "/spiffs/listen/lasers2/laser2.aac", 384 },
        { "/spiffs/listen/lasers2/laser3.aac", 384 },
        { "/spiffs/listen/lasers2/laser4.aac", 384 },
        { "/spiffs/listen/lasers2/laser5.aac", 384 },
        { "/spiffs/listen/lasers2/laser6.aac", 320 },
        { "/spiffs/listen/lasers2/laser7.aac", 384 },
        { "/spiffs/listen/lasers2/laser8.aac", 384 },
        { "/spiffs/listen/lasers2/laser9.aac", 384 },
    };

    constexpr SoundEffect Robot[] = {
        { "/spiffs/listen/robot/robot_1.aac", 256 },
        { "/spiffs/listen/robot/robot_2.aac", 384 },
        { "/spiffs/listen/robot/robot_3.aac", 320 },
        { "/spiffs/listen/robot/robot_4.aac", 256 },
    };

    struct SfxGroup { const char* name; std::span<const SoundEffect> clips; };
    constexpr SfxGroup SfxGroups[] = {
        { "lasers1", Lasers1 },
        { "lasers2", Lasers2 },
        { "robot",   Robot   },
    };

    PSRAMByteBuffer readFile(const char* path){
        std::ifstream f(path, std::ios::binary | std::ios::ate);
        if(!f){
            return {};
        }
        const std::streamsize size = f.tellg();
        if(size <= 0){
            return {};
        }
        f.seekg(0);
        PSRAMByteBuffer buf(static_cast<size_t>(size));
        f.read(reinterpret_cast<char*>(buf.data()), size);
        buf.resize(static_cast<size_t>(f.gcount()));
        return buf;
    }

    void buildAndPlayIntro(const Application* app){
        Audio* audio = app->getService<Audio>();
        AACAudioGenerator* aacGen = app->getService<AACAudioGenerator>();
        if(audio == nullptr || aacGen == nullptr || ServiceLocator::SpeechAudioGenInstance == nullptr || ServiceLocator::SpeechGenInstance == nullptr){
            return;
        }

        const StartPrompt& prompt = StartPrompts[rand() % std::size(StartPrompts)];
        // Fork: durations were measured with the stock voice; a faster/slower voice preset scales them, and the
        // prompt must still end when the recogniser is ready.
        const uint32_t promptMs = static_cast<uint32_t>(prompt.durationMs * Voice::current().stretch / Voice::StockStretch);
        const uint32_t budget = promptMs >= IntroWindowMs ? 0 : IntroWindowMs - promptMs;

        const SfxGroup& group = SfxGroups[rand() % std::size(SfxGroups)];

        const uint32_t preloadStart = millis();

        const size_t maxAttempts = group.clips.size() * 4;

        std::vector<Audio::GenSourcePair> batch;
        batch.reserve(maxAttempts + 1); // + 1 for the spoken prompt source

        std::vector<size_t> pool;
        pool.reserve(group.clips.size());
        uint32_t accumulated = 0;
        size_t attempts = 0;
        while(accumulated < budget && attempts < maxAttempts){
            ++attempts;
            if(pool.empty()){
                for(size_t i = 0; i < group.clips.size(); ++i){
                    pool.push_back(i);
                }
            }
            const size_t poolIdx = rand() % pool.size();
            const SoundEffect& sfx = group.clips[pool[poolIdx]];
            pool.erase(pool.begin() + poolIdx);

            // Preload the clip into PSRAM now (before loadModel() hammers the flash),
            // so the audio task can play it without blocking on the flash lock.
            PSRAMByteBuffer bytes = readFile(sfx.path);
            if(bytes.empty()){
                continue;
            }

            batch.emplace_back(aacGen, std::make_unique<MemoryAudioSource>(std::move(bytes)));
            accumulated += sfx.durationMs;
        }

        batch.emplace_back(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, prompt.text));

        audio->play(std::span<Audio::GenSourcePair>(batch));
    }

}

std::vector<AudioFrontend::Phrase> ListenState::phrases;

/*
 * Fork: the AI brain path. While the intro plays, a worker task joins the home WiFi and finds the brain on the
 * owner's PC; the robot then records one utterance (AFE + VAD only, no offline recognizer - faster and lighter),
 * the worker sends it and waits for the reply, and ListenState::tick() speaks it / runs the chosen action.
 * The worker always switches WiFi off when it's done. If WiFi or the brain isn't there, the robot falls back to
 * the offline command list for this listen.
 */
struct ListenState::AiSession {
	HomeWiFi wifi;
	HomeWiFi::Config cfg;
	std::atomic<int> link{ 0 };        // 0 connecting, 1 ready, -1 failed
	const char* linkError = "";
	std::shared_ptr<BrainLink::Recording> rec = std::make_shared<BrainLink::Recording>();
	std::atomic<bool> cancel{ false };
	std::atomic<bool> replied{ false };
	BrainLink::Reply reply;
	uint32_t ip = 0;
	uint16_t port = 0;
	char voice[16] = {};
	bool handled = false;
	bool recordingStopped = false;
	uint32_t heardAt = 0;       // when "Heard" went to the controller
	bool thinkingShown = false; // "Thinking" sent
};

namespace {
	std::atomic<bool> aiWorkerRunning{ false };

	void aiWorker(void* arg){
		auto* holder = static_cast<std::shared_ptr<ListenState::AiSession>*>(arg);
		std::shared_ptr<ListenState::AiSession> s = *holder;
		delete holder;

		const HomeWiFi::Result r = s->wifi.start(s->cfg, 7000);
		if(r != HomeWiFi::Result::Ok){
			s->linkError = HomeWiFi::resultName(r);
			s->link = -1;
		}else{
			uint32_t ip = s->cfg.brainIp;
			uint16_t port = s->cfg.brainPort;
			if(!BrainLink::reachable(ip, port, 700)){
				if(BrainLink::discover(ip, port, 1600)){
					HomeWiFi::saveBrain(ip, port);
				}else{
					ip = 0;
				}
			}
			if(ip == 0){
				s->linkError = "brain_not_found";
				s->link = -1;
			}else{
				s->ip = ip;
				s->port = port;
				s->link = 1;
				printf("[BBAI] event=linked rssi=%d\n", s->wifi.rssi());
				// Wait for the utterance (the AFE proc task fills s->rec), then ask the brain
				const uint32_t start = millis();
				while(!s->rec->done && !s->cancel && millis() - start < 20000){
					vTaskDelay(pdMS_TO_TICKS(50));
				}
				if(!s->cancel){
					if(!s->rec->heardSpeech){
						s->reply.error = "heard_nothing";
					}else{
						MemMark::mark("ai: sending");
						s->reply = BrainLink::converse(s->ip, s->port, s->rec->pcm.data(), s->rec->pcm.size(), s->voice, 25000);
					}
					s->replied = true;
				}
			}
		}
		s->wifi.stop();
		aiWorkerRunning = false;
		vTaskDelete(nullptr);
	}
}

void ListenState::preallocate(){
	phrases.reserve(activations.size() * 2);
}

ListenState::ListenState(BBStateMachine* sm) : State(sm){
	phrases.clear();
    ListenTiming::t0 = 0;
    ListenTiming::mark("enter");

    // Fork: AI brain path - start joining the WiFi right away, in parallel with the intro
    {
        const HomeWiFi::Config cfg = HomeWiFi::load();
        if(cfg.aiEnabled && !cfg.ssid.empty() && !aiWorkerRunning){
            ai = std::make_shared<AiSession>();
            ai->cfg = cfg;
            strncpy(ai->voice, Voice::current().name, sizeof(ai->voice) - 1);
            aiWorkerRunning = true;
            auto* holder = new std::shared_ptr<AiSession>(ai);
            // Internal-RAM stack: WiFi start writes PHY calibration to flash
            if(xTaskCreatePinnedToCore(aiWorker, "bbai", 6144, holder, 4, nullptr, 0) != pdPASS){
                delete holder;
                aiWorkerRunning = false;
                ai.reset();
            }
        }
    }

    const Application* app = ApplicationStatics::getApplication();

    buildAndPlayIntro(app);
    ListenTiming::mark("intro queued");

    auto buttonInput = app->getService<ISRButtonInput>();
    if(buttonInput == nullptr){
        ESP_LOGE(TAG, "ButtonInput service missing!");
        return;
    }

    sm->bindState(buttonInput->OnButtonEvent, this, &ListenState::onButtonEvent);

    if(!ServiceLocator::ScenarioRoutineServiceInstance){
        CMF_LOG(ListenState, LogLevel::Error, "ScenarioRoutineService missing in constructor");
        return;
    }

    ServiceLocator::ScenarioRoutineServiceInstance->reset();

    com = ApplicationStatics::getApplication()->getService<Com>();
    if(com == nullptr){
        ESP_LOGE(TAG, "Com service missing!");
        return;
    }

    sm->bindState(com->OnCommand, this, &ListenState::onCommand);
    ListenData listenData{.phase = ListenData::Phase::Listening};
    com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);


    AudioFrontend* audioFrontend = app->getService<AudioFrontend>();
    if(audioFrontend == nullptr){
        CMF_LOG(ListenState, LogLevel::Error, "AudioFrontend service missing in constructor");
        return;
    }

    if(ai){
        // Record as soon as the intro ends - WiFi keeps connecting in the background and the worker sends the
        // recording once the brain is reachable, so the owner never waits for the WiFi. tick() handles failures.
        audioFrontend->loadModel(false);
        audioFrontend->setMode(AudioFrontend::Mode::Off);
        ListenTiming::mark("afe loaded (ai)");
        if(Audio* audio = app->getService<Audio>()){
            audio->waitEnd(portMAX_DELAY);
        }
        audioFrontend->startRecording(ai->rec);
        ListenTiming::mark("LISTENING (ai)");
        return;
    }

    startOffline(app);
}

void ListenState::startOffline(const Application* app){
    AudioFrontend* audioFrontend = app->getService<AudioFrontend>();
    if(audioFrontend == nullptr){
        return;
    }
    audioFrontend->loadModel();
    ListenTiming::mark("model loaded");

    audioFrontend->setMode(AudioFrontend::Mode::Off);

    if(!activations.empty()){
        for(const ScenarioActivation& activation : activations){
            const RoutineFactory factory = routineForScenario({ activation.scenario, activation.scenarioData });
            phrases.push_back({ activation.string, activation.phonemes, activation.fuzzyCore, activation.fuzzyThreshold, reinterpret_cast<const void*>(factory) });
        }

        if(ServiceLocator::IRStorageInstance){
            for(uint8_t i = 0; i < ServiceLocator::IRStorageInstance->getCount(); ++i){
                const IRCommand& cmd = ServiceLocator::IRStorageInstance->getCommand(i);
                phrases.push_back({ cmd.phonemes, cmd.phonemes });
            }
        }

        audioFrontend->setSpeechPhrases(phrases);
        ListenTiming::mark("phrases set");
    }

    if(Audio* audio = app->getService<Audio>()){
        audio->waitEnd(portMAX_DELAY);
        ListenTiming::mark("intro finished");

        sm->bindState(audioFrontend->onPhrase, this, &ListenState::onPhrase);
        audioFrontend->setMode(AudioFrontend::Mode::Speech);
        ListenTiming::mark("LISTENING");
    }
}

void ListenState::fallBackToOffline(const Application* app, const char* why){
    printf("[BBAI] event=offline why=%s\n", why);
    AudioFrontend* audioFrontend = app->getService<AudioFrontend>();
    if(audioFrontend != nullptr){
        audioFrontend->setMode(AudioFrontend::Mode::Off);
        audioFrontend->unloadModel();
    }
    ai->cancel = true;
    ai.reset();
    // WiFi must be completely off before the offline recognizer loads: both need the same scarce internal RAM
    const uint32_t start = millis();
    while(aiWorkerRunning && millis() - start < 5000){
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    MemMark::mark("ai: fallback");
    if(Audio* audio = app->getService<Audio>()){
        if(ServiceLocator::SpeechAudioGenInstance && ServiceLocator::SpeechGenInstance){
            audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                        std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text,
                                                            "My brain is offline. Using backup circuits. Say that again."));
        }
    }
    startOffline(app);
}

ListenState::~ListenState(){
    // Event bindings are dropped by BBStateMachine::unbindStateEvents() after this dtor runs.
    if(ai){
        ai->cancel = true; // the worker switches WiFi off by itself
    }
    const Application* app = ApplicationStatics::getApplication();
    if(AudioFrontend* audioFrontend = app->getService<AudioFrontend>()){
        audioFrontend->setMode(AudioFrontend::Mode::Off);
		audioFrontend->unloadModel();
        ListenTiming::mark("model unloaded");
    }

    if(timedOut){
        playPhraseBlocking(app, Phrase::ListeningTimeout);
    }
}

int64_t ListenState::getDynamicTickInterval() const{
    return ai ? 50 : -1;
}

void ListenState::tick(float){
    if(!ai || ai->handled){
        return;
    }
    const Application* app = ApplicationStatics::getApplication();
    AudioFrontend* audioFrontend = app->getService<AudioFrontend>();

    if(ai->link == -1 && !ai->replied){
        fallBackToOffline(app, ai->linkError);
        return;
    }
    if(ai->rec->done && !ai->recordingStopped){
        ai->recordingStopped = true;
        if(audioFrontend){
            audioFrontend->setMode(AudioFrontend::Mode::Off);
            audioFrontend->unloadModel(); // ~17 KB of internal RAM back before WiFi has to send
        }
        printf("[BBAI] event=recorded ms=%u speech=%d\n", (unsigned) (ai->rec->pcm.size() / 16), ai->rec->heardSpeech ? 1 : 0);
        MemMark::mark("ai: recorded");
        if(com != nullptr && ai->rec->heardSpeech){
            ListenData listenData{.phase = ListenData::Phase::Heard};
            com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
            ai->heardAt = millis();
        }
    }
    // Controller: "Heard" for a moment, then "Thinking" while the PC works (once the robot is talking to it)
    if(ai->heardAt != 0 && !ai->thinkingShown && ai->link == 1 && millis() - ai->heardAt >= 1200 && com != nullptr){
        ai->thinkingShown = true;
        ListenData listenData{.phase = ListenData::Phase::Thinking};
        com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
    }
    if(!ai->replied){
        return;
    }
    ai->handled = true;
    const BrainLink::Reply& r = ai->reply;
    printf("[BBAI] event=reply ok=%d error=%s\n", r.ok ? 1 : 0, r.error.empty() ? "none" : r.error.c_str());
    if(!r.heard.empty()) printf("BBAI heard: %s\n", r.heard.c_str());
    if(!r.say.empty()) printf("BBAI says: %s\n", r.say.c_str());
    if(!r.action.empty()) printf("BBAI does: %s\n", r.action.c_str());

    if(!r.ok){
        if(r.error == "heard_nothing"){
            timedOut = true; // the dtor plays the usual "didn't hear you" line
        }else if(Audio* audio = app->getService<Audio>()){
            audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                        std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, "My brain isn't answering. Try again."));
            audio->waitEnd(portMAX_DELAY);
        }
        if(com != nullptr){
            ListenData listenData{.phase = ListenData::Phase::NotUnderstand};
            com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
        }
        sm->transitionTo<IdleState>();
        return;
    }

    if(!r.say.empty()){
        if(Audio* audio = app->getService<Audio>()){
            audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                        std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, r.say));
            audio->waitEnd(portMAX_DELAY);
        }
    }

    if(!r.action.empty()){
        for(size_t i = 0; i < activations.size(); ++i){
            if(r.action == activations[i].string){
                onPhrase(true, static_cast<int>(i), r.action, 1.0f);
                return;
            }
        }
        printf("[BBAI] event=unknown_action\n");
    }
    sm->transitionTo<IdleState>();
}

void ListenState::onRC(){
    sm->transitionTo<RCState>();
}

void ListenState::onPhrase(bool recognized, int index, const std::string& transcript, float confidence){
	// Runs on the AFE proc task. Do NOT stop the AFE or play audio here
    if(!recognized){
    	CMF_LOG(ListenState, LogLevel::Info, "Listen result: recognized=%d, index=%d, transcript='%s', confidence=%.2f",
			recognized, index, transcript.c_str(), confidence);

        if(com != nullptr){
            ListenData listenData{.phase = ListenData::Phase::NotUnderstand};
            com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
        }
        timedOut = true;
        sm->transitionTo<IdleState>();
        return;
    }

	if(index < 0){
        // Abort phrase
        aborted();
        sm->transitionTo<IdleState>();
        return;
    }

    if(!ServiceLocator::ScenarioRoutineServiceInstance){
        CMF_LOG(ListenState, LogLevel::Error, "ScenarioRoutineService missing in onPhrase");
        return;
    }

    if(index >= static_cast<int>(activations.size())){
        const int irIndex = index - static_cast<int>(activations.size());
        if(ServiceLocator::IRStorageInstance){
            ServiceLocator::IRStorageInstance->setSelectedIndex(static_cast<uint8_t>(irIndex));
        }
        ServiceLocator::ScenarioRoutineServiceInstance->setRoutineFactory(&makeRoutine<IRActionRoutine>);
        sm->transitionTo<ScenarioState>();
        return;
    }

    const ScenarioActivation& activation = activations[index];
    const RoutineFactory factory = routineForScenario({ activation.scenario, activation.scenarioData });
    if(factory == nullptr){
        CMF_LOG(ListenState, LogLevel::Error, "No routine mapped for scenario %d", static_cast<int>(activation.scenario));
        sm->transitionTo<IdleState>();
        return;
    }

    ServiceLocator::ScenarioRoutineServiceInstance->setRoutineFactory(factory);
    sm->transitionTo<ScenarioState>();
}

void ListenState::onButtonEvent(int button, ISRButtonInput::Action action){
    if(action != ISRButtonInput::Action::Press || button != (int)Button::Power){
        return;
    }
    aborted();
    sm->transitionTo<IdleState>();
}

void ListenState::onCommand(Ctrl::Command cmd) {
    if(cmd == Ctrl::Command::Listen || cmd == Ctrl::ShutUp){
        aborted();
        sm->transitionTo<IdleState>();
    	return;
    }
}

void ListenState::aborted(){
	// Previously gated on ScenarioRoutineService (unrelated) and passed Phrases::get()'s -1 "no phrase"
	// result straight into map(), and could hand play() a null generator.
	if(ServiceLocator::SpeechAudioGenInstance && ServiceLocator::SpeechGenInstance){
		if(Audio* audio = ApplicationStatics::getApplication()->getService<Audio>()){
			const int16_t id = Phrases::get(Phrase::ListenAbort);
			const std::string text = id >= 0 ? Phrases::map(Phrase::ListenAbort, id) : std::string();
			audio->stop();
			if(!text.empty()){
				audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text));
			}
		}
	}

    if(com == nullptr){
        return;
    }

    ListenData listenData{.phase = ListenData::Phase::Abort};
    com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
}
