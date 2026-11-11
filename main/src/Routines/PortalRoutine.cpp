#include "PortalRoutine.h"
#include <Statics/ApplicationStatics.h>
#include <Core/Application.h>
#include <Services/Audio/Audio.h>
#include <Services/Audio/AACAudioGenerator.h>
#include <Services/Audio/FileAudioSource.h>
#include <BBData.h>
#include <Util/stdafx.h>
#include "Services/Com.h"

DEFINE_LOG(PortalRoutine)

Routine::TickingState PortalRoutine::tick(float deltaTime){
	const Application* app = ApplicationStatics::getApplication();
	Com* com = app->getService<Com>();
	if(com == nullptr){
		CMF_LOG(PortalRoutine, LogLevel::Error, "Com service missing");
		return TickingState::Done;
	}

	const uint64_t start = millis();

	OpenPortalData portal{};
	portal.durationMs = PortalDurationMs;
	com->sendData(BB::State::Scenario, BB::Action::Scenario::OpenPortal, portal);

	Audio* audio = app->getService<Audio>();
	AACAudioGenerator* aacGen = app->getService<AACAudioGenerator>();
	if(audio != nullptr && aacGen != nullptr){
		audio->play(aacGen, std::make_unique<FileAudioSource>("/spiffs/listen/lasers1/phaseJump3.aac"));
		audio->waitEnd(portMAX_DELAY);
	}

	// Hold until the controller's animation is over
	const uint64_t elapsed = millis() - start;
	if(elapsed < PortalDurationMs){
		delayMillis(PortalDurationMs - elapsed);
	}

	return TickingState::Done;
}
