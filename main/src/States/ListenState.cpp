#include "Routines/UserRoutine.h"
#include "Services/UserData.h"
#include "Services/Conversation.h"
#include "Services/Personal.h"
#include <esp_heap_caps.h>
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
#include "Audio/Beeps.h"
#include "Util/PSRAMAllocator.h"
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <mutex>
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

// Fork: the last AI conversation, for the controller's Robot status page
namespace {
std::mutex lastConvLock;
std::string lastHeard;
std::string lastSaid;
}
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

    // Fork: spoken = false for conversation-mode follow-ups: one short sound says "your turn", no "Yes?" every time
    void buildAndPlayIntro(const Application* app, bool spoken = true){
        Audio* audio = app->getService<Audio>();
        AACAudioGenerator* aacGen = app->getService<AACAudioGenerator>();
        if(audio == nullptr || aacGen == nullptr || ServiceLocator::SpeechAudioGenInstance == nullptr || ServiceLocator::SpeechGenInstance == nullptr){
            return;
        }

        StartPrompt prompt = StartPrompts[rand() % std::size(StartPrompts)];
        // Fork: the owner's own "I'm listening" line (Personalize > Summon, "is tapped"). Its length is estimated
        // from the stock prompts (~75 ms per character with the stock voice); Personalize keeps these lines short.
        const std::string own = spoken ? Personal::summonText(Personal::SummonMoment::Listen) : std::string();
        if(!own.empty()){
            prompt.text = own.c_str();
            prompt.durationMs = static_cast<uint16_t>(std::min<size_t>(own.size() * 75 + 150, IntroWindowMs));
        }
        // Fork: durations were measured with the stock voice; a faster/slower voice preset scales them, and the
        // prompt must still end when the recogniser is ready.
        const uint32_t promptMs = static_cast<uint32_t>(prompt.durationMs * Voice::stretch() / Voice::StockStretch);
        const uint32_t budget = !spoken ? 1 : (promptMs >= IntroWindowMs ? 0 : IntroWindowMs - promptMs);

        const SfxGroup& group = SfxGroups[rand() % std::size(SfxGroups)];

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

        if(spoken){
            batch.emplace_back(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, prompt.text));
        }
        if(batch.empty()){
            return;
        }

        audio->play(std::span<Audio::GenSourcePair>(batch));
    }

}

std::vector<AudioFrontend::Phrase> ListenState::phrases;
std::vector<UserData::Command> ListenState::commandSnapshot;

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
	std::atomic<bool> recordingStopped{ false }; // the recording is done and the listening engine unloaded
	// Start WiFi only once recordingStopped: WiFi (~25 KB of internal RAM) and the listening engine (~17 KB) together
	// leave ~12 KB, and the WiFi driver loses a few hundred bytes per start/stop - after a handful of back-to-back
	// listens esp_wifi_init found no 3.3 KB block and the robot aborted. Set when RAM is short or in a conversation.
	bool wifiAfterRecording = false;
	uint32_t heardAt = 0;       // when "Heard" went to the controller
	bool thinkingShown = false; // "Thinking" sent
	uint32_t blipAt = 0;        // last "still thinking" beep
};

namespace {
	std::atomic<bool> aiWorkerRunning{ false };

	/*
	 * After the AI brain fails, listens skip WiFi for a while and go straight to the built-in commands. Loading the
	 * offline recognizer right after a WiFi session (same listen) could run internal RAM to zero, after which WiFi
	 * wouldn't start again until a reboot - so a failed AI listen just ends, and the next press is the offline one.
	 */
	constexpr uint32_t AiCooldownMs = 2 * 60 * 1000;        // after a WiFi problem (worth retrying soon)
	constexpr uint32_t ShortCooldownMs = 20 * 1000;        // router slow to hand out an address
	constexpr uint32_t BrainOffCooldownMs = 3 * 60 * 1000; // brain not there: usually the PC or the app is just off
	std::atomic<uint32_t> aiCooldownUntil{ 0 }; // also cleared from the console task ("ai on")

	// Joining WiFi while recording is only safe with this much internal RAM before the listening engine loads
	constexpr size_t OverlapMinFree = 58 * 1024;
	constexpr size_t OverlapMinBlock = 24 * 1024;

	bool aiCoolingDown(){
		const uint32_t until = aiCooldownUntil;
		return until != 0 && (int32_t) (millis() - until) < 0;
	}

	void aiWorker(void* arg){
		auto* holder = static_cast<std::shared_ptr<ListenState::AiSession>*>(arg);
		std::shared_ptr<ListenState::AiSession> s = *holder;
		delete holder;

		if(s->wifiAfterRecording){
			const uint32_t waitStart = millis();
			while(!s->recordingStopped && !s->cancel && millis() - waitStart < 25000){
				vTaskDelay(pdMS_TO_TICKS(30));
			}
		}
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
						// canLook: the brain may ask for a photo ("what do you see?"); the camera is free while listening
						s->reply = BrainLink::converse(s->ip, s->port, s->rec->pcm.data(), s->rec->pcm.size(), s->voice, 25000, true);
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

bool ListenState::aiBusy(){
    return aiWorkerRunning.load();
}

void ListenState::clearAiPause(){
    if(aiCooldownUntil.exchange(0) != 0){
        printf("[BBAI] event=pause_cleared\n");
    }
}

void ListenState::preallocate(){
	phrases.reserve(activations.size() * 2);
}

ListenState::ListenState(BBStateMachine* sm) : State(sm){
	phrases.clear();

    // Fork: AI brain path - start joining the WiFi right away, in parallel with the intro
    {
        const HomeWiFi::Config cfg = HomeWiFi::load();
        if(cfg.aiEnabled && aiCoolingDown()){
            printf("[BBAI] event=skipped why=cooldown s_left=%u\n", (unsigned) ((aiCooldownUntil.load() - millis()) / 1000));
        }
        if(cfg.aiEnabled && !cfg.ssid.empty() && !aiWorkerRunning && !aiCoolingDown()){
            ai = std::make_shared<AiSession>();
            ai->cfg = cfg;
            // Before the listening engine loads: is there room to join WiFi while it records? (see AiSession)
            const size_t freeRam = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
            const size_t block = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
            const bool followUp = Conversation::active() && Conversation::turns.load() > 0;
            ai->wifiAfterRecording = followUp || freeRam < OverlapMinFree || block < OverlapMinBlock;
            if(ai->wifiAfterRecording){
                printf("[BBAI] event=wifi_after_recording free=%u block=%u conv=%d\n", (unsigned) (freeRam / 1024),
                       (unsigned) (block / 1024), followUp ? 1 : 0);
            }
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

    // Fork: conversation mode - the first turn gets the usual prompt, later ones just a short sound
    const bool followUp = Conversation::active() && Conversation::turns.load() > 0;
    if(Conversation::active()){
        ++Conversation::turns;
        printf("[BBCONV] event=listen turn=%d ram=%u block=%u\n", Conversation::turns.load(),
               (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
               (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024));
    }
    buildAndPlayIntro(app, !followUp);

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
        if(Audio* audio = app->getService<Audio>()){
            audio->waitEnd(portMAX_DELAY);
        }
        audioFrontend->startRecording(ai->rec);
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

        // Fork: the owner's own voice commands, after the IR ones (onPhrase counts on this order)
        {
            std::lock_guard<std::mutex> g(UserData::lock);
            commandSnapshot = UserData::commands; // the phrase list points into this copy while listening
        }
        for(const UserData::Command& cmd : commandSnapshot){
            phrases.push_back({ cmd.phrase.c_str(), cmd.phonemes.c_str() });
        }

        audioFrontend->setSpeechPhrases(phrases);
    }

    if(Audio* audio = app->getService<Audio>()){
        audio->waitEnd(portMAX_DELAY);

        sm->bindState(audioFrontend->onPhrase, this, &ListenState::onPhrase);
        audioFrontend->setMode(AudioFrontend::Mode::Speech);
    }
}

// What the robot says when the brain can't be used: what's wrong, and whether it needs plugging into Dimension Hopper
static bool brainProblem(const std::string& why){
    // Nobody answering on the PC side - most often Dimension Hopper simply isn't running (stopped, closed, PC asleep)
    return why == "brain_not_found" || why == "brain_unreachable" || why == "no_reply" || why == "empty_reply";
}

static uint32_t cooldownFor(const std::string& why){
    if(why == "no_ip_address") return ShortCooldownMs; // the router was slow once: usually fine on the next try
    return brainProblem(why) ? BrainOffCooldownMs : AiCooldownMs;
}

// What the robot says when the brain can't be used. Problems the owner must fix get a "plug me in"; the brain just
// being off is normal and gets a calm one-liner.
static const char* problemSentence(const std::string& why){
    if(why == "network_not_found" || why == "not_configured")
        return "I can't find my WiFi network. Plug me into Dimension Hopper to check my WiFi settings.";
    if(why == "wrong_password")
        return "My WiFi password doesn't work any more. Plug me into Dimension Hopper and send it again.";
    if(brainProblem(why))
        return "My brain is offline, so I'm using my backup circuits. Ask me again.";
    return "I can't reach the WiFi right now. Ask me again, I'll use my backup circuits.";
}

void ListenState::fallBackToOffline(const Application* app, const char* why){
    printf("[BBAI] event=offline why=%s cooldown_s=%u\n", why, (unsigned) (cooldownFor(why) / 1000));
    AudioFrontend* audioFrontend = app->getService<AudioFrontend>();
    if(audioFrontend != nullptr){
        audioFrontend->setMode(AudioFrontend::Mode::Off);
        audioFrontend->unloadModel();
    }
    ai->cancel = true;
    ai->handled = true;
    HomeWiFi::setLastProblem(why);
    aiCooldownUntil = (millis() + cooldownFor(why)) | 1; // never 0 (0 = no pause)
    // Let the worker switch WiFi off before anything else needs the memory
    const uint32_t start = millis();
    while(aiWorkerRunning && millis() - start < 5000){
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    MemMark::mark("ai: fallback");
    if(com != nullptr){
        ListenData listenData{.phase = ListenData::Phase::NotUnderstand};
        com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
    }
    if(Audio* audio = app->getService<Audio>()){
        if(ServiceLocator::SpeechAudioGenInstance && ServiceLocator::SpeechGenInstance){
            audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                        std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, problemSentence(why)));
            audio->waitEnd(portMAX_DELAY);
        }
    }
    sm->transitionTo<IdleState>();
}

void ListenState::sayLine(const Application* app, const char* line){
    if(Audio* audio = app->getService<Audio>(); audio != nullptr && ServiceLocator::SpeechAudioGenInstance){
        audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                    std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, line));
        audio->waitEnd(portMAX_DELAY);
    }
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
    }

    if(timedOut){
        if(Conversation::active()){
            // Conversation mode: a quiet turn just listens again; the last allowed one ends the chat
            if(Conversation::quietTurn()) sayLine(app, ConversationOverLine);
        }else{
            playPhraseBlocking(app, Phrase::ListeningTimeout);
        }
    }
}

// Fork: how often the soft "still thinking" pulse plays while it connects and the brain works on an answer
static constexpr uint32_t ThinkingBlipMs = 1200;

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
        if(audioFrontend){
            audioFrontend->setMode(AudioFrontend::Mode::Off);
            audioFrontend->unloadModel(); // ~17 KB of internal RAM back before WiFi has to send
        }
        ai->recordingStopped = true; // after the unload: a worker waiting for it starts WiFi now
        printf("[BBAI] event=recorded ms=%u speech=%d\n", (unsigned) (ai->rec->pcm.size() / 16), ai->rec->heardSpeech ? 1 : 0);
        MemMark::mark("ai: recorded");
        if(ai->rec->heardSpeech){
            // Fork: a chirp so you know it heard you (Personalize: "beeps"); soft blips follow until the answer
            Beeps::heard(app->getService<Audio>());
            ai->blipAt = millis();
        }
        if(com != nullptr && ai->rec->heardSpeech){
            ListenData listenData{.phase = ListenData::Phase::Heard};
            com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
            ai->heardAt = millis();
        }
    }
    if(ai->blipAt != 0 && !ai->replied && millis() - ai->blipAt >= ThinkingBlipMs){
        ai->blipAt = millis();
        Beeps::thinking(app->getService<Audio>());
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
    printf("[BBAI] event=reply ok=%d error=%s looked=%d\n", r.ok ? 1 : 0, r.error.empty() ? "none" : r.error.c_str(), r.looked ? 1 : 0);
    if(r.ok) HomeWiFi::setLastProblem("none");
    if(!r.heard.empty()) printf("BBAI heard: %s\n", r.heard.c_str());
    if(!r.say.empty()) printf("BBAI says: %s\n", r.say.c_str());
    if(!r.action.empty()) printf("BBAI does: %s\n", r.action.c_str());

    if(!r.ok){
        if(r.error == "heard_nothing"){
            timedOut = true; // the dtor plays the usual "didn't hear you" line (or counts a quiet conversation turn)
        }else{
            Conversation::end("brain_error");
            HomeWiFi::setLastProblem(r.error.c_str());
            aiCooldownUntil = (millis() + cooldownFor(r.error)) | 1;
            if(Audio* audio = app->getService<Audio>()){
                audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                            std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, problemSentence(r.error)));
                audio->waitEnd(portMAX_DELAY);
            }
        }
        if(com != nullptr){
            ListenData listenData{.phase = ListenData::Phase::NotUnderstand};
            com->sendData(BB::State::Listen, BB::Action::Listen{}, listenData);
        }
        sm->transitionTo<IdleState>();
        return;
    }

    {
        std::lock_guard<std::mutex> guard(lastConvLock);
        lastHeard = r.heard;
        lastSaid = r.say;
    }

    // Controller: subtitles - what it heard and what it's about to say
    if(com != nullptr && (!r.heard.empty() || !r.say.empty())){
        SubtitleData subtitle;
        subtitle.heard = r.heard;
        subtitle.say = r.say;
        com->sendData(BB::State::Scenario, BB::Action::Scenario::Subtitle, subtitle);
    }

    if(!r.say.empty()){
        if(Audio* audio = app->getService<Audio>()){
            audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
                        std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, r.say));
            audio->waitEnd(portMAX_DELAY);
        }
    }

    // Fork: conversation mode - a goodbye ends it (after the reply), and so do too many turns with nothing heard
    if(Conversation::active()){
        if(r.heard.empty()){
            if(Conversation::quietTurn()) sayLine(app, ConversationOverLine);
        }else{
            Conversation::heardSomething();
            if(Conversation::isGoodbye(r.heard)) Conversation::end("goodbye");
        }
    }

    if(!r.action.empty()){
        // WiFi holds ~25 KB of internal RAM until the worker has switched it off. An action can start right away
        // when the reply has nothing to say, and a dance loads its speech model at once: together they ran the
        // robot out of internal memory. Same wait as the fallback path.
        const uint32_t start = millis();
        while(aiWorkerRunning && millis() - start < 5000){
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        MemMark::mark("ai: action");
        for(size_t i = 0; i < activations.size(); ++i){
            if(r.action == activations[i].string){
                onPhrase(true, static_cast<int>(i), r.action, 1.0f);
                return;
            }
        }
        // Fork: one of the owner's own voice commands (the brain gets their phrases as actions)
        if(const int routine = UserData::routineForPhrase(r.action); routine >= 0 && ServiceLocator::ScenarioRoutineServiceInstance){
            startUserRoutine(routine);
            return;
        }
        printf("[BBAI] event=unknown_action\n");
    }
    sm->transitionTo<IdleState>();
}

void ListenState::lastConversation(std::string& heard, std::string& said){
    std::lock_guard<std::mutex> guard(lastConvLock);
    heard = lastHeard;
    said = lastSaid;
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
        Conversation::end("cancel");
        aborted();
        sm->transitionTo<IdleState>();
        return;
    }

    Conversation::heardSomething();

    if(!ServiceLocator::ScenarioRoutineServiceInstance){
        CMF_LOG(ListenState, LogLevel::Error, "ScenarioRoutineService missing in onPhrase");
        return;
    }

    const int irCount = ServiceLocator::IRStorageInstance ? static_cast<int>(ServiceLocator::IRStorageInstance->getCount()) : 0;
    if(index >= static_cast<int>(activations.size()) + irCount){
        const int cmd = index - static_cast<int>(activations.size()) - irCount;
        if(cmd < static_cast<int>(commandSnapshot.size())){
            startUserRoutine(commandSnapshot[cmd].routine);
        }else{
            sm->transitionTo<IdleState>();
        }
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

void ListenState::startUserRoutine(int routine){
    printf("[BBCMDS] event=heard routine=%d\n", routine);
    UserRoutine::pending = routine;
    ServiceLocator::ScenarioRoutineServiceInstance->setRoutineFactory(&makeRoutine<UserRoutine>);
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
    if(cmd == Ctrl::Command::Converse){
        Conversation::start(); // already listening: this listen is the first turn
        return;
    }
    // Fork: in conversation mode any button stops listening (main.cpp ends the conversation itself)
    const bool convButton = (Conversation::active() || Conversation::justEnded()) &&
                            (cmd == Ctrl::Command::Poke || cmd == Ctrl::Command::Summon || cmd == Ctrl::Command::EnterRC);
    if(cmd == Ctrl::Command::Listen || cmd == Ctrl::ShutUp || convButton){
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
