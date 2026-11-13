#include "ChatRoutine.h"
#include <Statics/ApplicationStatics.h>
#include <Core/Application.h>
#include <Services/Audio/Audio.h>
#include <BBData.h>
#include <Phrases.h>
#include <Scenarios.h>
#include <Util/ServiceLocator.h>
#include "Audio/SpeechAudioGen.h"
#include "Audio/SpeechAudioSource.h"
#include "Audio/SpeechGen.h"
#include "Services/Com.h"

DEFINE_LOG(ChatRoutine)

Routine::TickingState speakChatLine(uint8_t index){
	const Application* app = ApplicationStatics::getApplication();
	Audio* audio = app->getService<Audio>();
	Com* com = app->getService<Com>();
	if(audio == nullptr || !ServiceLocator::SpeechAudioGenInstance || !ServiceLocator::SpeechGenInstance || com == nullptr){
		CMF_LOG(ChatRoutine, LogLevel::Error, "Missing required service(s)");
		return Routine::TickingState::Done;
	}

	const std::string text = Phrases::map(Phrase::Chat, index);
	if(index >= ChatLineCount || text.empty()){
		CMF_LOG(ChatRoutine, LogLevel::Warning, "No chat line %u", index);
		return Routine::TickingState::Done;
	}

	ChatData chatData{};
	chatData.id = index;
	com->sendData(BB::State::Scenario, BB::Action::Scenario::Chat, chatData);

	audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text));
	audio->waitEnd(portMAX_DELAY);

	return Routine::TickingState::Done;
}
