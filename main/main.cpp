#include <Core/EntryPoint.h>
#include "driver/gpio.h"
#include <nvs_flash.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <string>
#include <esp_app_desc.h>
#include <cstdlib>
#include <iterator>
#include <memory>
#include <Devices/SC7A20.h>
#include "States/BBStateMachine.h"
#include "Devices/BM8563.h"
#include "Periphery/I2CMaster.h"
#include "Periphery/I2S.h"
#include "Periphery/GPIOPeriph.h"
#include "Devices/TCA9555.h"
#include "Drivers/Input/InputTCA.h"
#include "Drivers/Input/InputGPIO.h"
#include "Drivers/Output/OutputTCA.h"
#include "Drivers/Output/OutputGPIO.h"
#include "Devices/Camera.h"
#include "FileSystem/SPIFFS.h"
#include "Services/Audio/AACAudioGenerator.h"
#include "Services/Audio/Audio.h"
#include "Services/Audio/FileAudioSource.h"
#include "Services/Modules/ModuleService.h"
#include "Drivers/Output/OutputPWM.h"
#include "src/Services/LEDModuleControl.h"
#include "Audio/MicInput.h"
#include "Pins.hpp"
#include "Util/HardwareConfiguration.h"
#include "EventBag.h"
#include "Event/EventBroadcaster.h"
#include "src/Services/Time.h"
#include "Audio/SpeechAudioGen.h"
#include "Audio/SpeechAudioSource.h"
#include "Audio/SpeechGen.h"
#include "Battery/Battery.h"
#include "Services/Time.h"
#include "Services/BaseBoard.h"
#include "States/IdleState.h"
#include "States/ListenState.h"
#include "States/RCState.h"
#include "States/ScenarioState.h"
#include "Services/ScenarioRoutineService.h"
#include "Periph/Bluetooth.h"
#include "BLE/GAP.h"
#include "BLE/Client.h"
#include "BLE/Server.h"
#include "Services/Com.h"
#include "Services/Settings.h"
#include "src/Services/IRStorage.h"
#include "Phone/Phone.h"
#include <Services/MotionService.h>
#include "Enums.h"
#include <hal/brownout_hal.h>
#include "Services/ISRButtonInput.h"
#include "Services/ShutdownService.h"
#include "Devices/Timer.h"
#include "Services/ObjDet.h"
#include <Phrases.h>
#include "Audio/SpeechAudioSource.h"
#include "Util/EfuseMeta.h"
#include "JigHWTest/JigHWTest.h"
#include "Services/GasConfigureService.h"
#include "FaceDet.h"
#include "Util/ServiceLocator.h"
#include "Audio/SoundEffects.h"

DEFINE_LOG(Butterbot)

class Butterbot : public Application {
	GENERATED_BODY(Butterbot, Application, void)

public:
	Butterbot() : Application(CONFIG_CMF_APPLICATION_TICK_INTERVAL / portTICK_PERIOD_MS,
							  CONFIG_CMF_APPLICATION_STACK_SIZE,
							  CONFIG_CMF_APPLICATION_THREAD_PRIORITY,
							  CONFIG_CMF_APPLICATION_CPU_CORE,
							  /*internalStack=*/true){}

protected:
	virtual void begin() noexcept override{
		Phrases::preallocate();
		ListenState::preallocate();

		esp_log_level_set("*", ESP_LOG_WARN);
		// Pairing is low-volume and hard to debug blind: keep the BLE connection story on the console.
		esp_log_level_set("BLE_GAP", ESP_LOG_INFO);
		esp_log_level_set("BLE_ConMan", ESP_LOG_INFO);

		// Stack overflow otherwise - increase Threaded/AsyncEntity stack size if logging is needed
		esp_log_level_set("Android", ESP_LOG_NONE);
		esp_log_level_set("AMS", ESP_LOG_NONE);
		esp_log_level_set("ANCS", ESP_LOG_NONE);
		esp_log_level_set("Com", ESP_LOG_NONE);
		esp_log_level_set("MotionService", ESP_LOG_NONE);
		esp_log_level_set("BaseBoard", ESP_LOG_NONE);
		esp_log_level_set("AudioFrontend", ESP_LOG_NONE);
		esp_log_level_set("FaceDet", ESP_LOG_NONE);
		esp_log_level_set("Time", ESP_LOG_NONE);


		esp_err_t ret = nvs_flash_init();
		if(ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND){
			ESP_ERROR_CHECK(nvs_flash_erase());
			ret = nvs_flash_init();
		}
		ESP_ERROR_CHECK(ret);

		if(!SPIFFS::init()){
			CMF_LOG(Butterbot, LogLevel::Error, "SPIFFS init failed, aborting startup");
			return;
		}

		gpio_install_isr_service(ESP_INTR_FLAG_IRAM);

		if(JigHWTest::checkJig()){
			printf("Jig\n");
			StrongObjectPtr<JigHWTest> test = newObject<JigHWTest>();
			test->start();
			vTaskDelete(nullptr);
		}else{
			printf("Hello\n");
		}

		if(!EfuseMeta::check()){
			while(true){
				vTaskDelay(1000);
				EfuseMeta::log();
			}
		}

		uint64_t partialInitTimeStartMs = millis();

		uint64_t partialInitTimeEndMs = millis();

		// Power button hold check
		static constexpr uint64_t HoldTimeMs = 1000;
		static constexpr uint64_t BootTimeMs = 306; // Measured boot time with oscilloscope
		uint64_t partialInitTimeMs = partialInitTimeEndMs - partialInitTimeStartMs; // First part of init
		uint64_t RemainingMs = HoldTimeMs - BootTimeMs - partialInitTimeMs;
		static constexpr uint64_t PollIntervalMs = 10;

		static constexpr gpio_config_t btnConfig = {
			.pin_bit_mask = 1ULL << PIN_BTN,
			.mode = GPIO_MODE_INPUT
		};
		gpio_config(&btnConfig);

		CMF_LOG(Butterbot, LogLevel::Info, "Boot was %lld ms, remaining hold time: %lld ms", BootTimeMs + partialInitTimeMs, RemainingMs);

		uint64_t deadline = millis() + RemainingMs;

		while(millis() < deadline){
			if(gpio_get_level(static_cast<gpio_num_t>(PIN_BTN)) == 0){
				CMF_LOG(Butterbot, LogLevel::Info, "Power button released early, shutting down");
				ShutdownService::PowerOff();
				return;
			}
			delayMillis(PollIntervalMs);
		}

		I2CMaster* i2c_main = registerPeriphery<I2CMaster>(I2CPort::Zero, static_cast<gpio_num_t>(I2C_MAIN_SDA), static_cast<gpio_num_t>(I2C_MAIN_SCL));
		I2CMaster* i2c_umax = registerPeriphery<I2CMaster>(I2CPort::One, static_cast<gpio_num_t>(I2C_UMAX_SDA), static_cast<gpio_num_t>(I2C_UMAX_SCL));

		TCA9555* tca = registerDevice<TCA9555>(i2c_main, HardwareConfiguration::getTcaAddress());
		OutputTCA* tcaOutput = registerDriver<OutputTCA>(HardwareConfiguration::getTcaOutputPins(), tca);
		InputTCA* tcaInput = registerDriver<InputTCA>(HardwareConfiguration::getTcaInputPins(), tca);

		GPIOPeriph* gpio = registerPeriphery<GPIOPeriph>();

		I2S* i2s_in = registerPeriphery<I2S>(I2S_NUM_0, HardwareConfiguration::getMicI2SConfig());
		I2S* i2s_out = registerPeriphery<I2S>(I2S_NUM_1, HardwareConfiguration::getAudioI2SConfig());

		Audio* audio = registerService<Audio>(i2s_out, OutputPin{tcaOutput, EXP_SD_MODE_PIN}, /*internalStack=*/true);
		audio->setGain(DefaultGain);

		ServiceLocator::SpeechGenInstance = std::make_unique<SpeechGen>();
		ServiceLocator::SpeechAudioGenInstance = std::make_unique<SpeechAudioGen>();

		Camera* cam = registerDevice<Camera>(HardwareConfiguration::getCameraConfig(static_cast<int>(i2c_main->getPort())), i2c_main, [](sensor_t* sensor){
			sensor->set_hmirror(sensor, 0);
			sensor->set_vflip(sensor, 1);
		});
		cam->setFormat(PIXFORMAT_RGB565);
		cam->setRes(FRAMESIZE_128X128);
		if(cam->init() != ESP_OK){
			CMF_LOG(Butterbot, LogLevel::Error, "Camera init failed, shutting down");
			failInit(audio, Phrase::CameraFailure);
			return;
		}

		if(cam->getFrame() == nullptr){
			CMF_LOG(Butterbot, LogLevel::Error, "Camera frame capture failed, shutting down");
			failInit(audio, Phrase::CameraFailure);
			return;
		}
		cam->releaseFrame();

		std::unique_ptr<I2CDevice> baseDevice = i2c_main->addDevice(HardwareConfiguration::getBaseBoardAddress());
		if(baseDevice == nullptr){
			CMF_LOG(Butterbot, LogLevel::Error, "BaseBoard not detected, shutting down");
			failInit(audio, Phrase::MotorBoardFailure);
			return;
		}

		BaseBoard* baseBoard = registerService<BaseBoard>(std::move(baseDevice), /*internalStack=*/false);

		LED<MonoLED, RGBLED>* ledService = registerService<LED<MonoLED, RGBLED>>(/*internalStack=*/false);
		ledService->reg(MonoLED::Status, OutputPin{tcaOutput, EXP_PIN_LED});

		ServiceLocator::LEDControlInstance = std::make_unique<LEDControl>(ledService);

		Battery* battery = registerService<Battery>(OutputPin{tcaOutput, EXP_CALIB_EN});
		battery->OnLevelChanged.bind(this, &Butterbot::onBatteryLevelEvent);
		battery->OnChargeStatus.bind(this, &Butterbot::onChargeStatusEvent);
		onBatteryLevelEvent(battery->getLevel());

		ServiceLocator::LEDControlInstance->startInit(); // After power button hold

		ServiceLocator::SettingsInstance = std::make_unique<Settings>();

		ServiceLocator::IRStorageInstance = std::make_unique<IRStorage>();

		OutputGPIO* gpioOutput = registerDriver<OutputGPIO>(HardwareConfiguration::getGpioOutputPins(), gpio);
		InputGPIO* gpioInput = registerDriver<InputGPIO>(HardwareConfiguration::getGpioInputPins(), gpio);

		OutputPWM* pwmOutput = registerDriver<OutputPWM>(HardwareConfiguration::getPwmOutputPins());

		ledService->reg(MonoLED::Module, OutputPin{pwmOutput, static_cast<int>(PWMChannel::ModuleLED)});
		ledService->reg(MonoLED::PIRIndicator, OutputPin{gpioOutput, CTRL_2});

		ModuleService* modules = registerService<ModuleService>(Modules::BusPins{
			.addr = {
				{ .driver = tcaInput, .port = EXP_ADDR_1 },
				{ .driver = tcaInput, .port = EXP_ADDR_2 },
				{ .driver = tcaInput, .port = EXP_ADDR_3 },
				{ .driver = tcaInput, .port = EXP_ADDR_4 },
				{ .driver = tcaInput, .port = EXP_ADDR_5 },
				{ .driver = tcaInput, .port = EXP_ADDR_6 },
			},
			.detPins = {
				{ .driver = tcaInput, .port = EXP_DET_1 },
				{ .driver = tcaInput, .port = EXP_DET_2 },
			},
			.i2c = i2c_umax,
			.subAddressPins = {
				{ .inputDriver = gpioInput, .outputDriver = pwmOutput, .inputPort = CTRL_1, .outputPort = (int)PWMChannel::ModuleLED, },
				{ .inputDriver = gpioInput, .outputDriver = gpioOutput, .inputPort = CTRL_2, .outputPort = CTRL_2 },
				{ .inputDriver = tcaInput, .outputDriver = tcaOutput, .inputPort = EXP_CTRL_3, .outputPort = EXP_CTRL_3 },
				{ .inputDriver = tcaInput, .outputDriver = tcaOutput, .inputPort = EXP_CTRL_4, .outputPort = EXP_CTRL_4 },
				{ .inputDriver = tcaInput, .outputDriver = tcaOutput, .inputPort = EXP_CTRL_5, .outputPort = EXP_CTRL_5 },
				{ .inputDriver = gpioInput, .outputDriver = gpioOutput, .inputPort = CTRL_6, .outputPort = CTRL_6 },
			}
		}, /*internalStack=*/true);

		GasConfigureService* gasService = registerService<GasConfigureService>();
		gasService->setOwner(modules);

		LEDModuleControl* ledModuleControl = registerService<LEDModuleControl>(ledService);

		ServiceLocator::ObjDetInstance = std::make_unique<ObjDet>();

		FaceDet* faceDet = registerService<FaceDet>(cam);

		ServiceLocator::MicInputInstance = std::make_unique<MicInput>(i2s_in);
		AudioFrontend* audioFrontend = registerService<AudioFrontend>();

		ServiceLocator::LEDControlInstance->registerToAudio(audio);

		AACAudioGenerator* aacGen = registerService<AACAudioGenerator>();

		BM8563* rtc = registerDevice<BM8563>(i2c_main, HardwareConfiguration::getRTCAddress());
		Time* timeService = registerService<Time>(rtc, /*internalStack=*/false);

		auto bt = new Bluetooth();
		auto gap = new BLE::GAP();
		auto client = new BLE::Client(gap);
		auto server = new BLE::Server(gap);

		srand(esp_random());

		auto com = registerService<Com>(server, /*internalStack=*/false);
		auto phone = registerService<Phone>(server, client);

		com->OnConnStatus.bind(this, &Butterbot::onConnStatus);
		com->OnCommand.bind(this, &Butterbot::onCommand);
		com->OnSettings.bind(this, &Butterbot::onSettings);

		server->start();

		speakAndWait(audio, Phrase::Startup);

		ServiceLocator::SC7A20Instance = std::make_unique<SC7A20>(i2c_main, HardwareConfiguration::getAcceleroAddress());
		MotionService* motionService = registerService<MotionService>();

		ISRButtonInput* input = registerService<ISRButtonInput>((int)Button::Power, InputPin{gpioInput, PIN_BTN}, /*internalStack=*/false);
		input->OnButtonEvent.bind(this, &Butterbot::onButtonEvent);
		input->OnButtonEvent.bind(modules, [this](int button, ISRButtonInput::Action action){
			if(action == ISRButtonInput::Action::Press){
				forceOffTimer = newObject<Timer>(this, ForcePowerOffTime, [](void*){ ShutdownService::PowerOff(); }, nullptr, "ForceOff");
				forceOffTimer->start();
			}else if(action == ISRButtonInput::Action::Release){
				if(forceOffTimer){
					forceOffTimer->stop();
					delete forceOffTimer.get();
					forceOffTimer = nullptr;
				}
			}
		});

		ServiceLocator::ScenarioRoutineServiceInstance = std::make_unique<ScenarioRoutineService>();

		ServiceLocator::ControllerStateInstance = std::make_unique<ControllerState>();

		EventBag* eventBag = registerService<EventBag>();

		BBStateMachine* stateMachine = registerService<BBStateMachine>();
		eventBag->setOwner(stateMachine);

		// Because of this, the starting state cannot be passed via constructor but has to be set later
		stateMachine->transitionTo<IdleState>();
		ServiceLocator::LEDControlInstance->startIdle();

		startStatusReporter();
	}

	virtual void tick(float deltaTime) noexcept override{
		if(pressed && millis() - lastPress >= ShutdownButtonPressTime){
			ShutdownService::Shutdown(ShutdownReason::Command);
		}
	}

	virtual TickType_t getEventScanningTime() const noexcept override{
		if(pressed){
			return 1000 / portTICK_PERIOD_MS;
		}

		return Super::getEventScanningTime();
	}

private:
	static constexpr uint64_t ShutdownButtonPressTime = 3000;
	static constexpr uint32_t ForcePowerOffTime = 10000;
	// Were uninitialized: a garbage 'true' could make tick() shut the robot down right after boot.
	bool pressed = false;
	uint64_t lastPress = 0;
	// Startup used 0.8 but unmute restored 1.0, so the volume jumped after every mute/unmute.
	static constexpr float DefaultGain = 0.8f;
	StrongObjectPtr<Timer> forceOffTimer;
	inline static bool muted = false;

	static constexpr Phrase ActivityModePhrases[] = { Phrase::SettingsActivityOff, Phrase::SettingsActivityChill, Phrase::SettingsActivityActive, Phrase::SettingsActivityHyper };
	static constexpr Phrase FrontSensorModePhrases[] = { Phrase::SettingsFrontSensorOff, Phrase::SettingsFrontSensorOn };
	static constexpr Phrase BottomSensorModePhrases[] = { Phrase::SettingsBottomSensorOff, Phrase::SettingsBottomSensorReduced, Phrase::SettingsBottomSensorOn };

private:
	/**
	 * Fork: one status line on the USB serial console every StatusIntervalMs, e.g.
	 *   [BBSTAT] dev=bot fw=v1.1.1-exadon.2 batt=73 charge=unplugged link=connected up=1234
	 * Dimension Hopper's "Check status" listens for it (without resetting the board) to show battery,
	 * charging and controller link. printf bypasses the log level, so it always appears.
	 */
	static constexpr uint64_t StatusIntervalMs = 5000;

	/*
	 * Diagnostics stream for Dimension Hopper's Diagnostics tab. Off by default; sending "diag on" over the
	 * serial port turns it on for DiagHoldMs (the app re-sends it while connected), "diag off" stops it.
	 * While on, a [BBDIAG] line with live readings is printed every DiagIntervalMs.
	 * Runs in its own low-priority task (not an esp_timer callback) so slow UART output never delays timers.
	 */
	static constexpr uint64_t DiagIntervalMs = 200;
	static constexpr uint64_t DiagHoldMs = 15000;
	uint64_t diagUntil = 0;
	std::atomic<uint8_t> lastMotion{ 0xFF };
	std::atomic<uint64_t> lastMotionMs{ 0 };

	void startStatusReporter(){
		if(MotionService* motion = getService<MotionService>()){
			motion->onMotion.bind(this, [this](MotionType type){
				lastMotion = static_cast<uint8_t>(type);
				lastMotionMs = millis();
			});
		}
		xTaskCreatePinnedToCore([](void* self){ static_cast<Butterbot*>(self)->diagTask(); }, "bbdiag", 4096, this, 1,
		                        nullptr, tskNO_AFFINITY);
	}

	void diagTask(){
		// Console stdin: never block the task waiting for input
		const int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
		fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

		std::string line;
		uint64_t nextStatus = millis() + StatusIntervalMs;
		uint64_t nextDiag = 0;
		for(;;){
			char buf[32];
			const int n = read(STDIN_FILENO, buf, sizeof(buf));
			for(int i = 0; i < n; ++i){
				if(buf[i] == '\n' || buf[i] == '\r'){
					if(line == "diag on") diagUntil = millis() + DiagHoldMs;
					else if(line == "diag off") diagUntil = 0;
					line.clear();
				}else if(line.size() < 32){
					line += buf[i];
				}
			}

			const uint64_t now = millis();
			if(now >= nextStatus){
				nextStatus = now + StatusIntervalMs;
				printStatus();
			}
			if(now < diagUntil && now >= nextDiag){
				nextDiag = now + DiagIntervalMs;
				printDiag();
			}
			vTaskDelay(pdMS_TO_TICKS(50));
		}
	}

	void printDiag() const{
		const BaseBoard* base = getService<BaseBoard>();
		const BBStateMachine* sm = getService<BBStateMachine>();
		const Battery* battery = getService<Battery>();
		const Com* com = getService<Com>();
		BaseBoard::Diag d{};
		if(base != nullptr){
			d = base->getDiag();
		}
		static constexpr const char* MotionNames[] = { "fall", "upsidedown", "pickup", "shake" };
		const uint8_t motion = lastMotion.load();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		printf("[BBDIAG] dev=bot state=%s front_raw=%d front=%d bottom=%d motor_l=%d motor_r=%d btn=%d batt=%d "
		       "link=%d motion=%s motion_age=%llu\n",
		       state != nullptr ? state->name() : "none",
		       d.frontRaw, d.frontFiltered, d.bottom, d.motorLeft, d.motorRight, pressed ? 1 : 0,
		       battery != nullptr ? static_cast<int>(battery->getPerc() + 0.5f) : -1,
		       com != nullptr && com->getStatus() == Com::ConnStatus::Connected ? 1 : 0,
		       motion < std::size(MotionNames) ? MotionNames[motion] : "none",
		       motion < std::size(MotionNames) ? (millis() - lastMotionMs.load()) / 1000 : 0);
	}

	void printStatus() const{
		const Battery* battery = getService<Battery>();
		const Com* com = getService<Com>();
		const char* charge = "unknown";
		if(battery != nullptr){
			switch(battery->getChargingState()){
				case ChargingState::Unplugged: charge = "unplugged"; break;
				case ChargingState::Charging: charge = "charging"; break;
				case ChargingState::Full: charge = "full"; break;
			}
		}
		printf("[BBSTAT] dev=bot fw=%s batt=%d charge=%s link=%s up=%llu\n",
		       esp_app_get_description()->version,
		       battery != nullptr ? static_cast<int>(battery->getPerc() + 0.5f) : -1,
		       charge,
		       com == nullptr ? "unknown" : (com->getStatus() == Com::ConnStatus::Connected ? "connected" : "disconnected"),
		       millis() / 1000);
	}

	static void speakAndWait(Audio* audio, Phrase phrase) {
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

	static void failInit(Audio* audio, Phrase failure) {
		speakAndWait(audio, failure);
		speakAndWait(audio, Phrase::ShuttingDown);
		ShutdownService::PowerOff();
	}

	void onConnStatus(Com::ConnStatus status){
		if(status != Com::ConnStatus::Connected){
			// Unmute BB on controller disconnect
			if(muted) toggleMute();
			return;
		}


		sendBatteryStatus();
		sendMuteStatus();
	}

	void onCommand(const Ctrl::Command command){
		if(command != Ctrl::ShutUp){
			return;
		}

		toggleMute();
	}

	void onSettings(const SettingsData& data){
		if(!ServiceLocator::SettingsInstance){
			return;
		}

		SettingsStruct settings = ServiceLocator::SettingsInstance->get();
		const bool frontChanged = settings.frontSensor != data.frontSensor;
		const bool bottomChanged = settings.bottomSensor != data.bottomSensor;
		const bool activityChanged = settings.activityMode != data.activityMode;
		if(!frontChanged && !bottomChanged && !activityChanged){
			return;
		}

		settings.frontSensor = data.frontSensor;
		settings.bottomSensor = data.bottomSensor;
		settings.activityMode = data.activityMode;
		ServiceLocator::SettingsInstance->set(settings);
		ServiceLocator::SettingsInstance->store();

		if(frontChanged || bottomChanged){
			if(BaseBoard* baseBoard = getService<BaseBoard>()){
				baseBoard->requestProximityState();
			}
		}

		Audio* audio = getService<Audio>();
		Com* com = getService<Com>();
		BBStateMachine* sm = getService<BBStateMachine>();
		if(audio == nullptr || com == nullptr || sm == nullptr){
			return;
		}

		if(sm->getActiveRoutine() != nullptr || audio->isPlaying()){
			return;
		}

		const size_t frontIndex = static_cast<size_t>(data.frontSensor);
		const size_t bottomIndex = static_cast<size_t>(data.bottomSensor);
		const size_t activityIndex = static_cast<size_t>(data.activityMode);
		bool spoken = false;

		if(frontChanged && frontIndex < std::size(FrontSensorModePhrases)){
			speakSettingsChange(audio, com, FrontSensorModePhrases[frontIndex]);
			spoken = true;
		}

		if(bottomChanged && bottomIndex < std::size(BottomSensorModePhrases)){
			speakSettingsChange(audio, com, BottomSensorModePhrases[bottomIndex]);
			spoken = true;
		}

		if(activityChanged && activityIndex < std::size(ActivityModePhrases)){
			speakSettingsChange(audio, com, ActivityModePhrases[activityIndex]);
			spoken = true;
		}

		if(spoken){
			com->sendData(BB::State::Idle, BB::Action::Idle::None);
		}
	}

	static void speakSettingsChange(Audio* audio, Com* com, Phrase phrase){
		if(!ServiceLocator::SpeechAudioGenInstance || !ServiceLocator::SpeechGenInstance){
			return;
		}

		const int16_t id = Phrases::get(phrase);
		if(id < 0){
			return;
		}

		com->sendData(BB::State::Idle, BB::Action::Idle::SettingsChange, SettingsChangeData{ .phrase = phrase, .id = static_cast<uint8_t>(id) });

		const std::string text = Phrases::map(phrase, id);
		if(text.empty()){
			return;
		}

		std::unique_ptr<SpeechAudioSource> source = std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text);
		audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::move(source));
		audio->waitEnd(portMAX_DELAY);
	}

	void toggleMute(){
		if(Audio* audio = getService<Audio>()){
			muted = !muted;
			audio->setGain(muted ? 0.f : DefaultGain);
			sendMuteStatus();

			if(!muted){
				playUnmuteSound(audio);
			}
		}
	}

	void playUnmuteSound(Audio* audio){
		BBStateMachine* sm = getService<BBStateMachine>();
		AACAudioGenerator* aacGen = getService<AACAudioGenerator>();
		if(sm == nullptr || aacGen == nullptr || sm->getActiveRoutine() != nullptr){
			return;
		}

		const char* path = SoundEffects::Random[rand() % std::size(SoundEffects::Random)];
		audio->play(aacGen, std::make_unique<FileAudioSource>(path));
	}

	void sendMuteStatus(){
		if(Com* com = getService<Com>()){
			com->sendData(BB::State::Idle, BB::Action::Idle::ShutUp, ShutUpData{ .muted = muted });
		}
	}

	// Sends current battery level + charge status to the controller as plain telemetry,
	// decoupled from the routine/animation system. Fired on connect, level change and
	// charge-status change.
	void sendBatteryStatus() const{
		auto* battery = getService<Battery>();
		auto* com = getService<Com>();
		if(battery == nullptr || com == nullptr){
			return;
		}

		static_assert((uint8_t)ChargeStatus::Unplugged == (uint8_t)ChargingState::Unplugged &&
					  (uint8_t)ChargeStatus::Charging == (uint8_t)ChargingState::Charging &&
					  (uint8_t)ChargeStatus::Full == (uint8_t)ChargingState::Full,
					  "ChargeStatus must mirror ChargingState");

		com->sendData(BB::State::Idle, BB::Action::Idle::BatteryLevel,
					  BatteryLevelData{ .level = (uint8_t)battery->getLevel(),
										.charge = (ChargeStatus)battery->getChargingState() });
	}

	void onChargeStatusEvent(ChargingState){
		sendBatteryStatus();
	}

	void onBatteryLevelEvent(Battery::Level level) const{
		sendBatteryStatus();

		if(level != Battery::Level::Critical){
			return;
		}

		// Decide on a live register read: Battery's cached charge state can be stale and would
		// otherwise shut down a robot that was just plugged in. (A failed read reports Unplugged,
		// so an I2C error still errs on the side of protecting the battery.)
		if(BaseBoard* baseBoard = getService<BaseBoard>()){
			if(baseBoard->getChargingState() != ChargingState::Unplugged){
				return;
			}
		}else if(const Battery* battery = getService<Battery>()){
			if(battery->getChargingState() != ChargingState::Unplugged){
				return;
			}
		}

		CMF_LOG(Butterbot, LogLevel::Warning, "Critical battery while unplugged, shutting down");
		ShutdownService::Shutdown(ShutdownReason::Battery);
	}

	void onButtonEvent(int button, ISRButtonInput::Action action){
		if(action == ISRButtonInput::Action::Press){
			pressed = true;
			lastPress = millis();
		}else if(action == ISRButtonInput::Action::Release){
			if(pressed && millis() - lastPress >= ShutdownButtonPressTime){
				ShutdownService::Shutdown(ShutdownReason::Command);
				return;
			}

			pressed = false;
		}
	}
};

CMF_MAIN(Butterbot)
