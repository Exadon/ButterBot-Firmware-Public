#include <Core/EntryPoint.h>
#include "Util/MemMark.h"
#include "driver/gpio.h"
#include <nvs_flash.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <string>
#include <esp_app_desc.h>
#include <esp_system.h>
#include <esp_gap_ble_api.h>
#include <nvs.h>
#include <algorithm>
#include <cstring>
#include <vector>
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
#include "Audio/VoicePreset.h"
#include "Services/HomeWiFi.h"
#include "Services/BrainLink.h"
#include <lwip/inet.h>
#include <Util/ServiceLocator.h>
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
#include <esp_heap_caps.h>

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
		// Recognition results (what the robot heard, and why a phrase was or wasn't matched). AFE_proc has the stack for it now.
		esp_log_level_set("AudioFrontend", ESP_LOG_INFO);
		esp_log_level_set("ListenState", ESP_LOG_INFO);
		esp_log_level_set("FaceDet", ESP_LOG_NONE);
		esp_log_level_set("Time", ESP_LOG_NONE);


		esp_err_t ret = nvs_flash_init();
		if(ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND){
			ESP_ERROR_CHECK(nvs_flash_erase());
			ret = nvs_flash_init();
		}
		ESP_ERROR_CHECK(ret);
		Voice::load(); // fork: the chosen voice preset, before anything speaks

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

		memMark("before I2S");
		I2S* i2s_in = registerPeriphery<I2S>(I2S_NUM_0, HardwareConfiguration::getMicI2SConfig());
		I2S* i2s_out = registerPeriphery<I2S>(I2S_NUM_1, HardwareConfiguration::getAudioI2SConfig());

		Audio* audio = registerService<Audio>(i2s_out, OutputPin{tcaOutput, EXP_SD_MODE_PIN}, /*internalStack=*/true);
		audio->setGain(DefaultGain);

		ServiceLocator::SpeechGenInstance = std::make_unique<SpeechGen>();
		ServiceLocator::SpeechAudioGenInstance = std::make_unique<SpeechAudioGen>();
		memMark("speech gen");

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
		memMark("camera");

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
		memMark("baseboard+batt");

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
		memMark("modules");

		LEDModuleControl* ledModuleControl = registerService<LEDModuleControl>(ledService);

		ServiceLocator::ObjDetInstance = std::make_unique<ObjDet>();
		memMark("objdet");

		FaceDet* faceDet = registerService<FaceDet>(cam);
		memMark("facedet");

		ServiceLocator::MicInputInstance = std::make_unique<MicInput>(i2s_in);
		AudioFrontend* audioFrontend = registerService<AudioFrontend>();
		memMark("audiofrontend");

		ServiceLocator::LEDControlInstance->registerToAudio(audio);

		AACAudioGenerator* aacGen = registerService<AACAudioGenerator>();
		memMark("aac");

		BM8563* rtc = registerDevice<BM8563>(i2c_main, HardwareConfiguration::getRTCAddress());
		Time* timeService = registerService<Time>(rtc, /*internalStack=*/false);

		auto bt = new Bluetooth();
		auto gap = new BLE::GAP();
		auto client = new BLE::Client(gap);
		auto server = new BLE::Server(gap);
		memMark("bluetooth");

		srand(esp_random());

		auto com = registerService<Com>(server, /*internalStack=*/false);
		auto phone = registerService<Phone>(server, client);
		memMark("com+phone");

		com->OnConnStatus.bind(this, &Butterbot::onConnStatus);
		com->OnCommand.bind(this, &Butterbot::onCommand);
		com->OnSettings.bind(this, &Butterbot::onSettings);

		server->start();

		speakAndWait(audio, Phrase::Startup);
		memMark("startup speech");

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
		printf("[BBMEM] after init: internal free %u KB (largest block %u KB, lowest %u KB), PSRAM free %u KB\n",
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) (heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
		MemMark::dumpTasks();
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
		countBoot();
		if(MotionService* motion = getService<MotionService>()){
			motion->onMotion.bind(this, [this](MotionType type){
				lastMotion = static_cast<uint8_t>(type);
				lastMotionMs = millis();
			});
		}
		// Stack in PSRAM: internal RAM is scarce and Bluetooth needs what's left (it never writes flash, so that's safe).
		xTaskCreatePinnedToCoreWithCaps([](void* self){ static_cast<Butterbot*>(self)->diagTask(); }, "bbdiag", 4096,
		                                this, 1, nullptr, tskNO_AFFINITY, MALLOC_CAP_SPIRAM);
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
					else if(line == "tasks"){ MemMark::mark("on request"); MemMark::dumpTasks(); }
					else if(line.rfind("drive ", 0) == 0) consoleDrive(line.c_str() + 6);
					else if(line == "stop") stopConsoleDrive("stop");
					else if(line == "forget") runFlashJob([]{ forgetBonds(); });
					else if(line == "voice") printVoice();
					else if(line.rfind("voice ", 0) == 0) runFlashJob([name = line.substr(6)]{ setVoice(name.c_str()); });
					else if(line.rfind("say ", 0) == 0) say(line.substr(4));
					else if(line == "wifi") runFlashJob([]{ printWifi(); }); // reads NVS strings from flash
					else if(line.rfind("wifi set ", 0) == 0) runFlashJob([args = line.substr(9)]{ setWifi(args.c_str()); });
					else if(line == "wifi clear") runFlashJob([]{ HomeWiFi::clear(); printWifi(); });
					else if(line == "wifi test") testWifi();
					else if(line == "wifi scan") scanWifi();
					else if(line.rfind("wifi bench ", 0) == 0) benchWifi(line.c_str() + 11);
					else if(line.rfind("brain ", 0) == 0) setBrain(line.c_str() + 6);
					else if(line == "ai on" || line == "ai off") runFlashJob([on = line == "ai on"]{ HomeWiFi::saveAiEnabled(on); printWifi(); });
					line.clear();
				}else if(line.size() < 300){
					line += buf[i];
				}
			}

			const uint64_t now = millis();
			if(driveUntil != 0 && now >= driveUntil){
				stopConsoleDrive("timeout");
			}
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

	/*
	 * The console task's stack lives in PSRAM, and reading or writing flash (NVS) switches the cache off, so doing it from it
	 * asserts and reboots the robot. Commands that save something run here instead: a one-shot task with an
	 * internal-RAM stack, one at a time (a second command while one runs waits its turn).
	 */
	static void runFlashJob(std::function<void()> job){
		static SemaphoreHandle_t lock = xSemaphoreCreateMutex();
		auto* heapJob = new std::function<void()>(std::move(job));
		const BaseType_t ok = xTaskCreatePinnedToCore([](void* arg){
			auto* fn = static_cast<std::function<void()>*>(arg);
			xSemaphoreTake(lock, portMAX_DELAY);
			(*fn)();
			xSemaphoreGive(lock);
			delete fn;
			vTaskDelete(nullptr);
		}, "bbflashjob", 4096, heapJob, 2, nullptr, tskNO_AFFINITY);
		if(ok != pdPASS){
			delete heapJob;
			printf("[BBMEM] flash job not started: out of memory\n");
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
		printf("[BBSTAT] dev=bot fw=%s batt=%d charge=%s link=%s up=%llu ram=%u ram_min=%u reset=%s boots=%lu\n",
		       esp_app_get_description()->version,
		       battery != nullptr ? static_cast<int>(battery->getPerc() + 0.5f) : -1,
		       charge,
		       com == nullptr ? "unknown" : (com->getStatus() == Com::ConnStatus::Connected ? "connected" : "disconnected"),
		       millis() / 1000,
		       // internal RAM in KB (now / lowest since boot): Bluetooth aborts the robot if this runs out
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) (heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       resetReasonName(), (unsigned long) bootCount);
	}

	/*
	 * Fork: driving from Dimension Hopper's Diagnostics tab over USB. "drive <left> <right>" (-100..100) runs
	 * the motors for DriveHoldMs; the app re-sends it while an arrow is held, so if the cable is pulled or the
	 * app hangs the robot stops by itself. Only in idle (in RC mode the controller is driving; elsewhere a
	 * routine owns the motors). Moving forward is refused while the front sensor sees an obstacle or the
	 * bottom sensor sees no floor. Replies with one [BBDRIVE] line.
	 */
	static constexpr uint64_t DriveHoldMs = 400;
	uint64_t driveUntil = 0;

	void consoleDrive(const char* args){
		int left = 0, right = 0;
		if(sscanf(args, "%d %d", &left, &right) != 2){
			printf("[BBDRIVE] result=error why=usage\n");
			return;
		}
		left = std::clamp(left, -100, 100);
		right = std::clamp(right, -100, 100);

		BaseBoard* base = getService<BaseBoard>();
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		if(base == nullptr){
			printf("[BBDRIVE] result=blocked why=no_motors\n");
			return;
		}
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			printf("[BBDRIVE] result=blocked why=busy state=%s\n", state != nullptr ? state->name() : "none");
			return;
		}
		const BaseBoard::Diag d = base->getDiag();
		if(left + right > 0 && (d.frontFiltered == 1 || d.bottom == 0)){
			stopConsoleDrive("blocked");
			printf("[BBDRIVE] result=blocked why=%s\n", d.frontFiltered == 1 ? "obstacle" : "no_floor");
			return;
		}
		base->setMotors(static_cast<int8_t>(left), static_cast<int8_t>(right));
		driveUntil = millis() + DriveHoldMs;
		printf("[BBDRIVE] result=ok l=%d r=%d\n", left, right);
	}

	void stopConsoleDrive(const char* why){
		if(driveUntil == 0){
			return;
		}
		driveUntil = 0;
		if(BaseBoard* base = getService<BaseBoard>()){
			base->setMotors(0, 0);
		}
		printf("[BBDRIVE] result=stopped why=%s\n", why);
	}

	// Fork: "forget" drops every Bluetooth bond, so the robot pairs from scratch with whichever controller asks next.
	static void forgetBonds(){
		int num = esp_ble_get_bond_device_num();
		int removed = 0;
		if(num > 0){
			std::vector<esp_ble_bond_dev_t> list(static_cast<size_t>(num));
			if(esp_ble_get_bond_device_list(&num, list.data()) == ESP_OK){
				for(int i = 0; i < num; ++i){
					if(esp_ble_remove_bond_device(list[static_cast<size_t>(i)].bd_addr) == ESP_OK) removed++;
				}
			}
		}
		printf("[BBFORGET] dev=bot removed=%d\n", removed);
	}

	/*
	 * Fork: voice presets over USB. "voice" lists them, "voice <name>" picks one (saved, used from the next
	 * sentence), "say <text>" speaks a line so Dimension Hopper can preview it. Replies: one [BBVOICE] line.
	 */
	static void printVoice(){
		std::string names;
		for(uint8_t i = 0; i < Voice::Count; ++i){
			if(i) names += ',';
			names += Voice::Presets[i].name;
		}
		printf("[BBVOICE] preset=%s names=%s\n", Voice::current().name, names.c_str());
	}

	static void setVoice(const char* which){
		const int i = Voice::find(which);
		if(i < 0){
			printf("[BBVOICE] result=error why=unknown_preset\n");
			return;
		}
		Voice::set(static_cast<uint8_t>(i));
		printVoice();
	}

	void say(const std::string& text){
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		Audio* audio = getService<Audio>();
		if(text.empty() || audio == nullptr || !ServiceLocator::SpeechAudioGenInstance || !ServiceLocator::SpeechGenInstance){
			printf("[BBVOICE] result=error why=no_speech\n");
			return;
		}
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			printf("[BBVOICE] result=busy state=%s\n", state != nullptr ? state->name() : "none");
			return;
		}
		audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
		            std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text));
		printf("[BBVOICE] result=saying preset=%s\n", Voice::current().name);
	}

	/*
	 * Fork: home WiFi + AI brain setup over USB (Dimension Hopper's AI tab). The SSID and password travel hex-encoded
	 * so spaces and symbols survive: "wifi set <hex ssid> <hex password>". "wifi test" joins the network, looks for the
	 * brain on the PC, reports RAM, and switches WiFi off again. Replies are [BBWIFI] lines (never the password).
	 */
	static std::string fromHex(const char* hex, size_t len){
		std::string out;
		if(len % 2 != 0) return {};
		for(size_t i = 0; i + 1 < len; i += 2){
			auto nib = [](char c) -> int { return c >= '0' && c <= '9' ? c - '0' : (c | 0x20) >= 'a' && (c | 0x20) <= 'f' ? (c | 0x20) - 'a' + 10 : -1; };
			const int hi = nib(hex[i]), lo = nib(hex[i + 1]);
			if(hi < 0 || lo < 0) return {};
			out += static_cast<char>(hi << 4 | lo);
		}
		return out;
	}

	static void printWifi(){
		const HomeWiFi::Config c = HomeWiFi::load();
		std::string ssidHex;
		static constexpr char Digits[] = "0123456789abcdef";
		for(unsigned char ch : c.ssid){ ssidHex += Digits[ch >> 4]; ssidHex += Digits[ch & 15]; }
		char brain[24] = "none";
		if(c.brainIp != 0){
			ip4_addr_t a{ .addr = c.brainIp };
			snprintf(brain, sizeof(brain), "%s", ip4addr_ntoa(&a));
		}
		printf("[BBWIFI] configured=%d ssid_hex=%s ai=%s brain=%s\n", c.ssid.empty() ? 0 : 1,
		       ssidHex.empty() ? "-" : ssidHex.c_str(), c.aiEnabled ? "on" : "off", brain);
	}

	static void setWifi(const char* args){
		const char* sp = strchr(args, ' ');
		const std::string ssid = fromHex(args, sp ? (size_t) (sp - args) : strlen(args));
		const std::string pass = sp ? fromHex(sp + 1, strlen(sp + 1)) : std::string();
		if(ssid.empty() || ssid.size() > 32 || pass.size() > 63 || (!pass.empty() && pass.size() < 8)){
			printf("[BBWIFI] result=error why=bad_credentials\n");
			return;
		}
		HomeWiFi::saveCredentials(ssid, pass);
		HomeWiFi::saveBrain(0, 0);
		printf("[BBWIFI] result=saved\n");
		printWifi();
	}

	std::atomic<bool> wifiTesting{ false };

	void testWifi(){
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			printf("[BBWIFI] result=busy state=%s\n", state != nullptr ? state->name() : "none");
			return;
		}
		if(wifiTesting.exchange(true)){
			printf("[BBWIFI] result=busy state=testing\n");
			return;
		}
		// Internal-RAM stack: starting WiFi writes PHY calibration data to flash
		xTaskCreatePinnedToCore([](void* self){
			auto* bot = static_cast<Butterbot*>(self);
			HomeWiFi wifi;
			const HomeWiFi::Result r = wifi.start(HomeWiFi::load(), 8000);
			if(r != HomeWiFi::Result::Ok){
				printf("[BBWIFI] result=failed why=%s ram=%u\n", HomeWiFi::resultName(r),
				       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
			}else{
				ip4_addr_t a{ .addr = wifi.ip() };
				char ipText[16];
				snprintf(ipText, sizeof(ipText), "%s", ip4addr_ntoa(&a));
				const unsigned ram = (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024);
				// The address Dimension Hopper gave us first (works across sub-networks), then a broadcast search
				const HomeWiFi::Config saved = HomeWiFi::load();
				uint32_t bip = saved.brainIp;
				uint16_t bport = saved.brainPort;
				bool brain = bip != 0 && BrainLink::reachable(bip, bport, 1500);
				if(!brain){
					brain = BrainLink::discover(bip, bport, 2000);
					if(brain) HomeWiFi::saveBrain(bip, bport);
				}
				char brainText[16] = "none";
				if(brain){
					ip4_addr_t b{ .addr = bip };
					snprintf(brainText, sizeof(brainText), "%s", ip4addr_ntoa(&b));
				}
				printf("[BBWIFI] result=connected ip=%s rssi=%d ram=%u ram_min=%u brain=%s\n", ipText, wifi.rssi(), ram,
				       (unsigned) (heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024), brainText);
			}
			wifi.stop();
			bot->wifiTesting = false;
			vTaskDelete(nullptr);
		}, "bbwifitest", 6144, this, 3, nullptr, 0);
	}

	/*
	 * "wifi scan": lists the networks the robot can see, so Dimension Hopper can offer them in a dropdown. One line:
	 * [BBWIFI] result=scan nets=<hex ssid>:<rssi>:<open 0/1>,... (strongest first). Same guards as "wifi test".
	 */
	void scanWifi(){
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			printf("[BBWIFI] result=busy state=%s\n", state != nullptr ? state->name() : "none");
			return;
		}
		if(wifiTesting.exchange(true)){
			printf("[BBWIFI] result=busy state=testing\n");
			return;
		}
		xTaskCreatePinnedToCore([](void* self){
			auto* bot = static_cast<Butterbot*>(self);
			{ // scope: everything is freed before the task deletes itself
				std::vector<HomeWiFi::Network> nets;
				HomeWiFi wifi;
				const bool ok = wifi.scan(nets);
				wifi.stop();
				if(!ok){
					printf("[BBWIFI] result=failed why=scan ram=%u\n", (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
				}else{
					std::string list;
					static constexpr char Digits[] = "0123456789abcdef";
					for(const auto& n : nets){
						if(!list.empty()) list += ',';
						for(unsigned char ch : n.ssid){ list += Digits[ch >> 4]; list += Digits[ch & 15]; }
						char tail[16];
						snprintf(tail, sizeof(tail), ":%d:%d", n.rssi, n.open ? 1 : 0);
						list += tail;
					}
					printf("[BBWIFI] result=scan count=%u nets=%s\n", (unsigned) nets.size(), list.empty() ? "-" : list.c_str());
				}
			}
			bot->wifiTesting = false;
			vTaskDelete(nullptr);
		}, "bbwifiscan", 6144, this, 3, nullptr, 0);
	}

	/*
	 * "brain <a.b.c.d> <port>": Dimension Hopper tells the robot where the brain is. Needed when the robot's WiFi and
	 * the PC are on different sub-networks (e.g. a mesh router with its own range), where the broadcast search can't
	 * reach. Saved; replies with the usual [BBWIFI] settings line.
	 */
	static void setBrain(const char* args){
		char ipText[20] = {};
		unsigned port = 0;
		ip4_addr_t a{};
		if(sscanf(args, "%19s %u", ipText, &port) != 2 || port == 0 || port > 65535 || !ip4addr_aton(ipText, &a)){
			printf("[BBWIFI] result=error why=usage\n");
			return;
		}
		const uint32_t ip = a.addr;
		runFlashJob([ip, port]{
			HomeWiFi::saveBrain(ip, static_cast<uint16_t>(port));
			printWifi();
		});
	}

	/*
	 * "wifi bench <ip> <port> <KB> <wifi|balance>": a diagnostic upload speed test. Joins the WiFi, sends KB kilobytes
	 * to a test receiver on the PC, and reports the time. The last word sets the WiFi/Bluetooth radio preference
	 * during the upload. One line: [BBWIFI] result=bench kb= ms= kbps= coex= ram= ram_min=
	 */
	struct BenchArgs {
		uint32_t ip;
		uint16_t port;
		uint32_t kb;
		bool preferWifi;
		Butterbot* bot;
	};

	void benchWifi(const char* args){
		char ipText[20] = {}, coex[12] = {};
		unsigned port = 0, kb = 0;
		ip4_addr_t a{};
		if(sscanf(args, "%19s %u %u %11s", ipText, &port, &kb, coex) != 4 || !ip4addr_aton(ipText, &a) || port == 0 ||
		   port > 65535 || kb == 0 || kb > 512){
			printf("[BBWIFI] result=error why=usage\n");
			return;
		}
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			printf("[BBWIFI] result=busy state=%s\n", state != nullptr ? state->name() : "none");
			return;
		}
		if(wifiTesting.exchange(true)){
			printf("[BBWIFI] result=busy state=testing\n");
			return;
		}
		auto* ba = new BenchArgs{ a.addr, static_cast<uint16_t>(port), kb, strcmp(coex, "wifi") == 0, this };
		xTaskCreatePinnedToCore([](void* arg){
			auto* ba = static_cast<BenchArgs*>(arg);
			{
				HomeWiFi wifi;
				const HomeWiFi::Result r = wifi.start(HomeWiFi::load(), 8000);
				if(r != HomeWiFi::Result::Ok){
					printf("[BBWIFI] result=failed why=%s\n", HomeWiFi::resultName(r));
				}else{
					const int64_t ms = BrainLink::uploadTest(ba->ip, ba->port, ba->kb * 1024, ba->preferWifi);
					const unsigned ram = (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024);
					const unsigned ramMin = (unsigned) (heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024);
					if(ms < 0){
						printf("[BBWIFI] result=bench_failed kb=%lu coex=%s rssi=%d ram=%u\n", (unsigned long) ba->kb,
						       ba->preferWifi ? "wifi" : "balance", wifi.rssi(), ram);
					}else{
						printf("[BBWIFI] result=bench kb=%lu ms=%lld kbps=%lld coex=%s rssi=%d ram=%u ram_min=%u\n",
						       (unsigned long) ba->kb, ms, ms > 0 ? (int64_t) ba->kb * 1000 / ms : 0,
						       ba->preferWifi ? "wifi" : "balance", wifi.rssi(), ram, ramMin);
					}
				}
				wifi.stop();
			}
			ba->bot->wifiTesting = false;
			delete ba;
			vTaskDelete(nullptr);
		}, "bbwifibench", 6144, ba, 3, nullptr, 0);
	}

	// Fork: why the robot last started and how many times it has booted (Dimension Hopper's health readout).
	uint32_t bootCount = 0;

	static const char* resetReasonName(){
		switch(esp_reset_reason()){
			case ESP_RST_POWERON: return "poweron";
			case ESP_RST_SW: return "software";
			case ESP_RST_PANIC: return "crash";
			case ESP_RST_INT_WDT:
			case ESP_RST_TASK_WDT:
			case ESP_RST_WDT: return "watchdog";
			case ESP_RST_BROWNOUT: return "brownout";
			case ESP_RST_DEEPSLEEP: return "sleep";
			case ESP_RST_EXT: return "external";
			case ESP_RST_USB: return "usb";
			default: return "other";
		}
	}

	void countBoot(){
		nvs_handle_t h;
		if(nvs_open("bbhealth", NVS_READWRITE, &h) != ESP_OK){
			return;
		}
		nvs_get_u32(h, "boots", &bootCount);
		bootCount++;
		nvs_set_u32(h, "boots", bootCount);
		nvs_commit(h);
		nvs_close(h);
	}

	// Fork: internal-RAM checkpoints during boot ([BBMEM] lines) - internal RAM running out crashes Bluetooth.
	static void memMark(const char* what){
		printf("[BBMEM] %-15s internal free %3u KB (largest %3u KB)\n", what,
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024));
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
