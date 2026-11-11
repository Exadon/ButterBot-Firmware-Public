#include "RCState.h"
#include <cmath>
#include <algorithm>
#include <Periphery/WiFi.h>
#include <Services/Audio/Audio.h>
#include <Services/Audio/AACAudioGenerator.h>
#include <Services/Audio/FileAudioSource.h>
#include <Services/ControllerState.h>
#include <Util/ServiceLocator.h>
#include "FaceDet.h"
#include "Audio/AudioFrontend.h"
#include "Services/Com.h"
#include "Util/WiFiStation.h"
#include "IdleState.h"
#include "Util/Feed.h"
#include "Services/LEDModuleControl.h"
#include "glm.hpp"
#include "gtx/vector_angle.inl"
#include "BBStateMachine.h"
#include "Audio/SoundEffects.h"

DEFINE_LOG(RCState)

RCState::RCState(BBStateMachine* sm) : State(sm){
	const Application* app = Application::getApp();

	if(LEDModuleControl* leds = app->getService<LEDModuleControl>()){
		leds->turnLedOff();
	}

	Com* com = app->getService<Com>();
	if(com == nullptr){
		CMF_LOG(RCState, Warning, "Com is nullptr in postInitProperties.");
		return;
	}

	sm->bindState(com->OnCommand, this, &RCState::onCommand);
	sm->bindState(com->OnDriveData, this, &RCState::onDriveData);
	sm->bindState(com->OnConnStatus, this, &RCState::onBTDisconnect);

	wifi = newObject<WiFi>(sm);
	if(wifi == nullptr){
		CMF_LOG(RCState, Warning, "WiFi is nullptr in postInitProperties.");
		return;
	}

	if(!ServiceLocator::ControllerStateInstance){
		CMF_LOG(RCState, Warning, "ControllerState is nullptr in postInitProperties.");
		return;
	}

	wifiSta = newObject<WiFiStation>(sm, *wifi, ServiceLocator::ControllerStateInstance->getSSID(), ServiceLocator::ControllerStateInstance->getPassword());

	sm->bindState(wifiSta->OnStationEvent, this, &RCState::onWifiDisconnect);

	wifiSta->connect();

	feed = newObject<Feed>(sm);
}

RCState::~RCState(){
	if(BaseBoard* baseBoard = Application::getApp()->getService<BaseBoard>()){
		baseBoard->setMotors(0, 0);
	}

	delete *feed;
	delete *wifiSta;
	delete *wifi;
}

int64_t RCState::getDynamicTickInterval() const noexcept{
	if(feed == nullptr){
		return -1;
	}

	if(!feed->isActive()){
		return 1000;
	}

	return 30;
}

void RCState::onCommand(Ctrl::Command cmd){
	if(cmd == Ctrl::Command::ExitRC){
		sm->transitionTo<IdleState>();
		return;
	}

	if(cmd == Ctrl::Command::RCSound){
		playRandomSound();
		return;
	}
}

void RCState::playRandomSound(){
	const Application* app = Application::getApp();

	Audio* audio = app->getService<Audio>();
	if(audio == nullptr){
		CMF_LOG(RCState, Warning, "Audio is nullptr in playRandomSound.");
		return;
	}

	AACAudioGenerator* aacGen = app->getService<AACAudioGenerator>();
	if(aacGen == nullptr){
		CMF_LOG(RCState, Warning, "AACAudioGenerator is nullptr in playRandomSound.");
		return;
	}

	if(audio->isPlaying()){
		return;
	}

	const char* path = SoundEffects::Random[rand() % std::size(SoundEffects::Random)];

	audio->play(aacGen, std::make_unique<FileAudioSource>(path));
}

void RCState::onDriveData(DriveData driveData){
	const Application* app = Application::getApp();

	BaseBoard* baseBoard = app->getService<BaseBoard>();
	if(baseBoard == nullptr){
		CMF_LOG(RCState, Warning, "BaseBoard is nullptr in event callback.");
		return;
	}

	if(driveData.joystickX == 0 && driveData.joystickY == 0){
		baseBoard->setMotors(driveData.joystickX, driveData.joystickY);
		return;
	}

	/*
	 * Proportional ("arcade") mixing. The stock code snapped the stick to one of 8 directions with fixed
	 * wheel ratios (and hit full speed at half deflection), so steering jumped between "straight", "hard
	 * curve" and "spin". Here speed follows how far the stick is pushed and turning blends continuously.
	 */
	static constexpr float MaxSpeed = 100.0f;
	static constexpr float MinDrive = 30.0f; // below this the motors stall, so any real push starts here
	static constexpr float TurnGain = 0.5f;  // how hard the stick's sideways part steers (1 = pivot on a wheel at 45 deg)

	const float x = glm::clamp(static_cast<float>(driveData.joystickX), -100.0f, 100.0f);
	const float y = glm::clamp(static_cast<float>(driveData.joystickY), -100.0f, 100.0f);
	const float ax = std::fabs(x);

	// Turn direction: normal when going forward, mirrored when reversing (like the stock 8-way mapping,
	// where stick back-right swings the robot the same way a car's rear does). Blend from +1 at the sideways
	// position to -1 at 45 degrees back, so there's no jump when the stick crosses the horizontal.
	float turnSign = y >= 0.0f ? 1.0f : -1.0f;
	if(ax >= 1.0f){
		turnSign = glm::clamp(1.0f + 2.0f * y / ax, -1.0f, 1.0f);
	}

	float leftSpeed = y + TurnGain * turnSign * x;
	float rightSpeed = y - TurnGain * turnSign * x;

	// Scale so the faster wheel runs at the stick's deflection (mapped onto MinDrive..MaxSpeed)
	const float peak = std::max(std::fabs(leftSpeed), std::fabs(rightSpeed));
	const float deflection = glm::clamp(std::sqrt(x * x + y * y) / 100.0f, 0.0f, 1.0f);
	if(peak < 1e-3f || deflection < 1e-3f){
		baseBoard->setMotors(0, 0);
		return;
	}
	const float target = MinDrive + (MaxSpeed - MinDrive) * deflection;
	leftSpeed = glm::clamp(leftSpeed / peak * target, -MaxSpeed, MaxSpeed);
	rightSpeed = glm::clamp(rightSpeed / peak * target, -MaxSpeed, MaxSpeed);

	baseBoard->setMotors(leftSpeed, rightSpeed);
}

void RCState::onBTDisconnect(Com::ConnStatus status){
	if(status != Com::ConnStatus::Disconnected){
		return;
	}

	sm->transitionTo<IdleState>();
}

void RCState::onWifiDisconnect(WiFiStation::EventType type, bool success){
	if(type != WiFiStation::EventType::Disconnect){
		return;
	}

	sm->transitionTo<IdleState>();
}
