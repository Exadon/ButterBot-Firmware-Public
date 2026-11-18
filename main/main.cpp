#include <Core/EntryPoint.h>
#include "Util/MemMark.h"
#include "Util/EventLog.h"
#include "driver/gpio.h"
#include <nvs_flash.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <fcntl.h>
#include <unistd.h>
#include <driver/uart.h>
#include <driver/uart_vfs.h>
#include <atomic>
#include <string>
#include <esp_app_desc.h>
#include <esp_system.h>
#include <esp_gap_ble_api.h>
#include <nvs.h>
#include <algorithm>
#include <cmath>
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
#include "Services/Personal.h"
#include "Services/UserData.h"
#include "Services/Conversation.h"
#include "Services/Time.h"
#include <Scenarios.h>
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
#include <Devices/Camera.h>
#include <img_converters.h>
#include "Util/ServiceLocator.h"
#include "Audio/SoundEffects.h"
#include <esp_heap_caps.h>
#include <mbedtls/base64.h>
#include <array>

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
		EventLog::install(); // fork: keep recent events for the problem report
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
		UserData::initStorage(); // fork: the "userdata" NVS partition (routines etc.), moved there from the main one
		Voice::load(); // fork: the chosen voice preset, before anything speaks
		Personal::load(); // fork: owner / robot names and the owner's ramble lines
		UserData::load(); // fork: the owner's routines, voice commands, schedules and quiet hours

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
		// Fork: Dimension Hopper's emulated controller drives over Bluetooth without RC mode (no camera hotspot).
		// The real controller only sends Drive in RC mode, where RCState handles it; here only idle reacts.
		com->OnDriveData.bind(this, &Butterbot::onIdleDrive);
		com->OnSettings.bind(this, &Butterbot::onSettings);

		server->start();

		// Fork: the owner's own start-up line (Personalize tab), else the stock one
		if(const std::string own = Personal::startupText(); !own.empty()) speakTextAndWait(audio, own);
		else speakAndWait(audio, Phrase::Startup);
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
		MemMark::watchFailedAllocs();
		printf("[BBMEM] after init: internal free %u KB (largest block %u KB, lowest %u KB), PSRAM free %u KB\n",
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) MemMark::lowestKb(),
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
	 * charging and controller link. printf bypasses the log level, so it always appears. Also: RAM, reset reason,
	 * boots, and settings storage ("nvs=" main NVS, "user=" routines etc., in 32-byte entries used/total).
	 * "stat" on the console prints one right away (Dimension Hopper's status strip).
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
		// Fork: read the console through the UART driver (an interrupt fills a RAM buffer). Read straight from the
		// 128-byte hardware FIFO, a command longer than that (a routine's text, a voice command) lost its tail.
		if(!uart_is_driver_installed(UART_NUM_0) && uart_driver_install(UART_NUM_0, 1024, 0, 0, nullptr, 0) == ESP_OK){
			uart_vfs_dev_use_driver(UART_NUM_0);
		}
		// Console stdin: never block the task waiting for input
		const int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
		fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

		std::string line;
		uint64_t nextStatus = millis() + StatusIntervalMs;
		uint64_t nextDiag = 0;
		for(;;){
			char buf[128];
			int n;
			while((n = read(STDIN_FILENO, buf, sizeof(buf))) > 0)
			for(int i = 0; i < n; ++i){
				if(buf[i] == '\n' || buf[i] == '\r'){
					if(line == "diag on") diagUntil = millis() + DiagHoldMs;
					else if(line == "diag off") diagUntil = 0;
					else if(line == "tasks"){ MemMark::mark("on request"); MemMark::dumpTasks(); }
					else if(line == "heaps"){ heap_caps_print_heap_info(MALLOC_CAP_INTERNAL); }
					else if(line == "emu on" || line == "emu off"){
						Com::mirrorToConsole = line == "emu on";
						printf("[BBEMU] mirror=%d\n", Com::mirrorToConsole ? 1 : 0);
					}
					else if(line.rfind("ctrl ", 0) == 0) consoleCtrl(line.c_str() + 5);
					else if(line == "events") printEvents();
					else if(line == "stat") nextStatus = 0; // fork: the status line now (Dimension Hopper's status strip)
					else if(line == "names") printNames();
					else if(line.rfind("names ", 0) == 0) runFlashJob([args = line.substr(6)]{ setNames(args); });
					else if(line == "ramble") printRambles();
					else if(line == "ramble clear") runFlashJob([]{ Personal::clearLines(); printRambles(); });
					else if(line.rfind("ramble add ", 0) == 0) runFlashJob([hex = line.substr(11)]{
						const std::string text = fromHex(hex.c_str(), hex.size());
						printf("[BBRAMBLE] result=%s\n", Personal::addLine(text) ? "added" : "full");
						printRambles();
					});
					else if(line.rfind("drive ", 0) == 0) consoleDrive(line.c_str() + 6);
					else if(line == "stop") stopConsoleDrive("stop");
					else if(line == "forget") runFlashJob([]{ forgetBonds(); });
					else if(line == "voice") printVoice();
					else if(line.rfind("voice tune ", 0) == 0) runFlashJob([args = line.substr(11)]{ tuneVoice(args); });
					else if(line == "startline") printStartLine();
					else if(line.rfind("startline ", 0) == 0) runFlashJob([hex = line.substr(10)]{
						Personal::setStartLine(hex == "-" ? std::string() : fromHex(hex.c_str(), hex.size()));
						printStartLine();
					});
					else if(line == "chat") printChat();
					else if(line == "summon") printSummon();
					else if(line.rfind("summon set ", 0) == 0) runFlashJob([args = line.substr(11)]{ setSummon(args); });
					else if(line == "cue") printCue();
					else if(line.rfind("cue set ", 0) == 0) runFlashJob([on = line.substr(8)]{
						Personal::setCue(on != "0");
						printCue();
					});
					else if(line == "clock") printClock();
					else if(line.rfind("clock set ", 0) == 0) runFlashJob([this, t = line.substr(10)]{ setClock(t); });
					else if(line == "rout") printRoutines();
					else if(line.rfind("rout ", 0) == 0) routineCommand(line.substr(5));
					else if(line == "cmds") printCommands();
					else if(line.rfind("cmds ", 0) == 0) runFlashJob([args = line.substr(5)]{ commandsCommand(args); });
					else if(line == "sched") printSchedules();
					else if(line.rfind("sched ", 0) == 0 || line.rfind("quiet ", 0) == 0)
						runFlashJob([args = line]{ schedulesCommand(args); });
					else if(line.rfind("chat set ", 0) == 0) runFlashJob([args = line.substr(9)]{ setChat(args); });
					else if(line.rfind("voice ", 0) == 0) runFlashJob([name = line.substr(6)]{ setVoice(name.c_str()); });
					else if(line.rfind("say ", 0) == 0) say(line.substr(4));
					else if(line.rfind("render ", 0) == 0) render(line.substr(7));
					else if(line == "wifi") runFlashJob([]{ printWifi(); }); // reads NVS strings from flash
					else if(line.rfind("wifi set ", 0) == 0) runFlashJob([args = line.substr(9)]{ setWifi(args.c_str()); });
					else if(line == "wifi clear") runFlashJob([]{ HomeWiFi::clear(); printWifi(); });
					else if(line == "wifi test") testWifi();
					else if(line == "wifi scan") scanWifi();
					else if(line == "cam") camFrame();
					else if(line == "srtest" || line.rfind("srtest ", 0) == 0) srTest(line.c_str() + 6);
					else if(line.rfind("wifi bench ", 0) == 0) benchWifi(line.c_str() + 11);
					else if(line.rfind("brain ", 0) == 0) setBrain(line.c_str() + 6);
					else if(line == "ai on" || line == "ai off") runFlashJob([on = line == "ai on"]{
						HomeWiFi::saveAiEnabled(on);
						if(on) ListenState::clearAiPause(); // the brain was just started: try it on the next listen
						printWifi();
					});
					line.clear();
				}else if(line.size() < 300){
					line += buf[i];
				}
			}

			MemMark::sample(); // every 50 ms: the real low point of internal RAM
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
		// Settings storage in 32-byte entries, used/total: "nvs" = the main NVS (Personalize, WiFi, Bluetooth, IR),
		// "user" = routines, commands and schedules (the same partition as nvs on robots without the "userdata" one)
		size_t nu = 0, nt = 0, uu = 0, ut = 0;
		UserData::usage(NVS_DEFAULT_PART_NAME, nu, nt);
		if(UserData::ownPart) UserData::usage(UserData::Part, uu, ut);
		printf("[BBSTAT] dev=bot fw=%s batt=%d charge=%s link=%s up=%llu ram=%u ram_min=%u alloc_fail=%lu reset=%s boots=%lu "
		       "nvs=%u/%u user=%u/%u\n",
		       esp_app_get_description()->version,
		       battery != nullptr ? static_cast<int>(battery->getPerc() + 0.5f) : -1,
		       charge,
		       com == nullptr ? "unknown" : (com->getStatus() == Com::ConnStatus::Connected ? "connected" : "disconnected"),
		       millis() / 1000,
		       // internal RAM in KB (now / lowest seen since boot): Bluetooth aborts the robot if this runs out
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
		       (unsigned) MemMark::lowestKb(), (unsigned long) MemMark::failedAllocs.load(),
		       resetReasonName(), (unsigned long) bootCount, (unsigned) nu, (unsigned) nt, (unsigned) uu, (unsigned) ut);
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

	/** Runs the motors for DriveHoldMs if the robot is idle and it's safe. nullptr = driving, else why not. */
	const char* driveIdle(int left, int right){
		left = std::clamp(left, -100, 100);
		right = std::clamp(right, -100, 100);

		BaseBoard* base = getService<BaseBoard>();
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		if(base == nullptr){
			return "no_motors";
		}
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			return "busy";
		}
		const BaseBoard::Diag d = base->getDiag();
		if(left + right > 0 && (d.frontFiltered == 1 || d.bottom == 0)){
			stopConsoleDrive("blocked");
			return d.frontFiltered == 1 ? "obstacle" : "no_floor";
		}
		base->setMotors(static_cast<int8_t>(left), static_cast<int8_t>(right));
		driveUntil = millis() + DriveHoldMs;
		return nullptr;
	}

	void consoleDrive(const char* args){
		int left = 0, right = 0;
		if(sscanf(args, "%d %d", &left, &right) != 2){
			printf("[BBDRIVE] result=error why=usage\n");
			return;
		}
		const char* why = driveIdle(left, right);
		if(why == nullptr){
			printf("[BBDRIVE] result=ok l=%d r=%d\n", std::clamp(left, -100, 100), std::clamp(right, -100, 100));
		}else if(strcmp(why, "busy") == 0){
			const BBStateMachine* sm = getService<BBStateMachine>();
			const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
			printf("[BBDRIVE] result=blocked why=busy state=%s\n", state != nullptr ? state->name() : "none");
		}else{
			printf("[BBDRIVE] result=blocked why=%s\n", why);
		}
	}

	/*
	 * Fork: personal touches from Dimension Hopper's Personalize tab. Names and lines travel hex-encoded so spaces
	 * and symbols survive: "names <hex owner|-> <hex robot|->", "ramble add <hex line>", "ramble clear".
	 * Replies: [BBNAMES] owner=<hex|-> robot=<hex|->, [BBRAMBLE] count=N max=N d=<hex>,<hex>,... (one line).
	 */
	static std::string toHex(const std::string& s){
		static constexpr char Digits[] = "0123456789abcdef";
		std::string out;
		for(const unsigned char c : s){
			out += Digits[c >> 4];
			out += Digits[c & 15];
		}
		return out.empty() ? "-" : out;
	}

	static void printNames(){
		std::string owner, robot;
		{
			std::lock_guard<std::mutex> g(Personal::lock);
			owner = Personal::ownerName;
			robot = Personal::robotName;
		}
		printf("[BBNAMES] owner=%s robot=%s\n", toHex(owner).c_str(), toHex(robot).c_str());
	}

	static void setNames(const std::string& args){
		const size_t sp = args.find(' ');
		const std::string a = args.substr(0, sp), b = sp == std::string::npos ? "-" : args.substr(sp + 1);
		auto decode = [](const std::string& h){ return h == "-" ? std::string() : fromHex(h.c_str(), h.size()); };
		Personal::setNames(decode(a), decode(b));
		printNames();
	}

	static void printRambles(){
		std::vector<std::string> copy;
		{
			std::lock_guard<std::mutex> g(Personal::lock);
			copy = Personal::lines;
		}
		// One line, so a one-shot console command gets everything: d=<hex>,<hex>,... ("-" when there are none)
		std::string all;
		for(size_t i = 0; i < copy.size(); ++i){
			if(i) all += ',';
			all += toHex(copy[i]);
		}
		printf("[BBRAMBLE] count=%u max=%u d=%s\n", (unsigned) copy.size(), (unsigned) Personal::MaxLines,
		       all.empty() ? "-" : all.c_str());
	}

	/** Fork: "ctrl <hex>" - a controller message from Dimension Hopper's emulated controller over USB. */
	void consoleCtrl(const char* hex){
		const std::string bytes = fromHex(hex, strlen(hex));
		Com* com = getService<Com>();
		if(com == nullptr || bytes.empty()){
			printf("[BBEMU] result=error\n");
			return;
		}
		com->inject(std::vector<uint8_t>(bytes.begin(), bytes.end()));
	}

	/** Fork: a Drive message while idle (the emulated controller). Same stick mixing as RC mode, simplified. */
	void onIdleDrive(DriveData data){
		const float x = std::clamp(static_cast<float>(data.joystickX), -100.0f, 100.0f);
		const float y = std::clamp(static_cast<float>(data.joystickY), -100.0f, 100.0f);
		if(std::fabs(x) < 1.0f && std::fabs(y) < 1.0f){
			stopConsoleDrive("released");
			return;
		}
		const float turnSign = y >= 0.0f ? 1.0f : -1.0f;
		float l = y + 0.5f * turnSign * x;
		float r = y - 0.5f * turnSign * x;
		const float peak = std::max(std::fabs(l), std::fabs(r));
		const float deflection = std::clamp(std::sqrt(x * x + y * y) / 100.0f, 0.0f, 1.0f);
		const float target = 30.0f + 70.0f * deflection;
		l = l / peak * target;
		r = r / peak * target;
		driveIdle(static_cast<int>(l), static_cast<int>(r));
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
		printf("[BBVOICE] preset=%s names=%s pitch=%d speed=%d max=%d\n", Voice::current().name, names.c_str(),
		       (int) Voice::pitchTune.load(), (int) Voice::speedTune.load(), Voice::MaxTune);
	}

	/** "voice tune <pitch %> <speed %>" - nudges on top of the preset. */
	static void tuneVoice(const std::string& args){
		int p = 0, s = 0;
		if(sscanf(args.c_str(), "%d %d", &p, &s) != 2 || !Voice::tune(p, s)){
			printf("[BBVOICE] result=error why=bad_tune\n");
			return;
		}
		printVoice();
	}

	/*
	 * Fork: the start-up line and the owner's chat replies (Personalize tab), hex-encoded like the names.
	 * "startline" / "startline <hex|->" -> [BBSTART] d=<hex|->
	 * "chat" / "chat set <index> <hex|->" -> [BBCHAT] count=<lines> d=<index>:<hex>,... (only changed ones; "-" if none)
	 */
	static void printStartLine(){
		std::string line;
		{
			std::lock_guard<std::mutex> g(Personal::lock);
			line = Personal::startLine;
		}
		printf("[BBSTART] d=%s\n", toHex(line).c_str());
	}

	static void printChat(){
		std::string all;
		for(size_t i = 0; i < ChatLineCount; ++i){
			const std::string r = Personal::chatReply(i);
			if(r.empty()) continue;
			if(!all.empty()) all += ',';
			all += std::to_string(i) + ":" + toHex(r);
		}
		printf("[BBCHAT] count=%u d=%s\n", (unsigned) ChatLineCount, all.empty() ? "-" : all.c_str());
	}

	static void setChat(const std::string& args){
		const size_t sp = args.find(' ');
		const int index = atoi(args.c_str());
		const std::string hex = sp == std::string::npos ? "-" : args.substr(sp + 1);
		if(index < 0 || index >= ChatLineCount){
			printf("[BBCHAT] result=error why=bad_index\n");
			return;
		}
		Personal::setChat((size_t) index, hex == "-" ? std::string() : fromHex(hex.c_str(), hex.size()));
		printChat();
	}

	/*
	 * Fork: the problem report (Dimension Hopper). "events" -> [BBEV] up=<s> n=<lines> d=<hex of "<uptime s> <line>\n"...>
	 * The robot's recent event lines since it switched on (EventLog), oldest first.
	 */
	static void printEvents(){
		const std::string all = EventLog::dump();
		size_t n = 0;
		for(char c : all) n += c == '\n';
		printf("[BBEV] up=%llu n=%u d=%s\n", (unsigned long long) (esp_timer_get_time() / 1000000), (unsigned) n,
		       all.empty() ? "-" : toHex(all).c_str());
		EventLog::syncConsole();
	}

	/*
	 * Fork: the owner's Summon lines and the heard/thinking beeps (Personalize tab).
	 * "summon" / "summon set <index> <hex|->" -> [BBSUMMON] count=<moments> d=<index>:<hex>,... ("-" if none)
	 *   index: 0 starts searching, 1 found you, 2 couldn't find you, 3 charging, 4 tapped (before a voice command); "|" separates alternatives
	 * "cue" / "cue set <0|1>" -> [BBCUE] on=<0|1>
	 */
	static void printSummon(){
		std::string all;
		for(size_t i = 0; i < (size_t) Personal::SummonMoment::Count; ++i){
			std::string lines;
			{
				std::lock_guard<std::mutex> g(Personal::lock);
				lines = Personal::summonLines[i];
			}
			if(lines.empty()) continue;
			if(!all.empty()) all += ',';
			all += std::to_string(i) + ":" + toHex(lines);
		}
		printf("[BBSUMMON] count=%u d=%s\n", (unsigned) Personal::SummonMoment::Count, all.empty() ? "-" : all.c_str());
	}

	static void setSummon(const std::string& args){
		const size_t sp = args.find(' ');
		const int index = atoi(args.c_str());
		const std::string hex = sp == std::string::npos ? "-" : args.substr(sp + 1);
		if(index < 0 || index >= (int) Personal::SummonMoment::Count){
			printf("[BBSUMMON] result=error why=bad_index\n");
			return;
		}
		Personal::setSummon((size_t) index, hex == "-" ? std::string() : fromHex(hex.c_str(), hex.size()));
		printSummon();
	}

	static void printCue(){
		printf("[BBCUE] on=%d\n", Personal::cueOn ? 1 : 0);
	}

	/*
	 * Fork: the robot's clock (Routines tab). The RTC keeps local wall-clock time; it travels as seconds since 1970
	 * read as if that wall clock were UTC (what Dimension Hopper sends from the PC's local time).
	 * "clock" / "clock set <seconds>" -> [BBCLOCK] t=<seconds> set=<0|1>
	 */
	void printClock(){
		Time* time = getService<Time>();
		if(time == nullptr){
			printf("[BBCLOCK] result=error why=no_clock\n");
			return;
		}
		tm now = time->getTime();
		const time_t t = mktime(&now); // TZ is unset on the robot: mktime == timegm
		printf("[BBCLOCK] t=%lld set=%d\n", (long long) t, time->isConfigured() ? 1 : 0);
	}

	void setClock(const std::string& arg){
		Time* time = getService<Time>();
		const long long t = atoll(arg.c_str());
		if(time == nullptr || t < 1704067200LL || t > 4102444800LL){ // 2024 .. 2100
			printf("[BBCLOCK] result=error why=bad_time\n");
			return;
		}
		time->setTime((time_t) t);
		printClock();
	}

	/*
	 * Fork: the owner's routines (Routines tab). Texts can be longer than a console line, so a save comes in parts:
	 * "rout begin <i>", "rout put <hex>" (repeated), "rout save" - an empty text deletes it. Replies are [BBROUT]:
	 *   "rout"            -> max=<n> d=<i>:<hex name>,...  ("-" when there are none)
	 *   "rout get <i>"    -> i=<i> d=<hex text|->
	 *   "rout run <i>"    -> result=queued i=<i> (it starts when the robot is idle) | result=busy state=<s>
	 *   "rout begin/put/save" -> result=begin | result=part n=<bytes> | result=saved i= steps= | result=error why=
	 * While a routine runs: event=start i=, event=end i= how=done|interrupted|charging|not_upright|stopped|missing.
	 */
	static inline std::string routStage;
	static inline int routStageIndex = -1;

	static void printRoutines(){
		std::string all;
		for(size_t i = 0; i < UserData::MaxRoutines; ++i){
			const std::string name = UserData::routineName(i);
			if(name.empty()) continue;
			if(!all.empty()) all += ',';
			all += std::to_string(i) + ":" + toHex(name);
		}
		printf("[BBROUT] max=%u d=%s\n", (unsigned) UserData::MaxRoutines, all.empty() ? "-" : all.c_str());
	}

	void routineCommand(const std::string& args){
		const int i = atoi(args.c_str() + (args.find(' ') == std::string::npos ? args.size() : args.find(' ') + 1));
		if(args.rfind("get ", 0) == 0){
			if(i < 0 || i >= (int) UserData::MaxRoutines){
				printf("[BBROUT] result=error why=bad_index\n");
				return;
			}
			printf("[BBROUT] i=%d d=%s\n", i, toHex(UserData::routineText((size_t) i)).c_str());
			EventLog::syncConsole();
		}else if(args.rfind("run ", 0) == 0){
			const BBStateMachine* sm = getService<BBStateMachine>();
			const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
			if(i < 0 || i >= (int) UserData::MaxRoutines || UserData::routineName((size_t) i).empty()){
				printf("[BBROUT] result=error why=no_routine\n");
			}else if(state == nullptr || strcmp(state->name(), "idle") != 0){
				printf("[BBROUT] result=busy state=%s\n", state != nullptr ? state->name() : "none");
			}else{
				UserData::runRequest = i;
				printf("[BBROUT] result=queued i=%d\n", i);
			}
		}else if(args.rfind("begin ", 0) == 0){
			routStage.clear();
			routStageIndex = i >= 0 && i < (int) UserData::MaxRoutines ? i : -1;
			if(routStageIndex < 0) printf("[BBROUT] result=error why=bad_index\n");
			else printf("[BBROUT] result=begin\n");
		}else if(args.rfind("put ", 0) == 0){
			const std::string part = fromHex(args.c_str() + 4, args.size() - 4);
			if(routStageIndex < 0 || part.empty() || routStage.size() + part.size() > UserData::MaxRoutineText){
				printf("[BBROUT] result=error why=part\n");
				return;
			}
			routStage += part;
			printf("[BBROUT] result=part n=%u\n", (unsigned) routStage.size());
		}else if(args == "save"){
			const int index = routStageIndex;
			std::string text = std::move(routStage);
			routStage.clear();
			routStageIndex = -1;
			const char* why = "";
			const int steps = text.empty() ? 0 : UserData::validate(text, &why);
			if(index < 0 || steps < 0){
				printf("[BBROUT] result=error why=%s\n", index < 0 ? "no_begin" : why);
				return;
			}
			runFlashJob([index, steps, text = std::move(text)]{
				if(UserData::setRoutine((size_t) index, text)) printf("[BBROUT] result=saved i=%d steps=%d\n", index, steps);
				else printf("[BBROUT] result=error why=space\n");
			});
		}else{
			printf("[BBROUT] result=error why=usage\n");
		}
	}

	/*
	 * Fork: the owner's voice commands. "cmds" -> [BBCMDS] max=<n> d=<routine>:<hex phrase>:<hex phonemes>,... ("-")
	 * "cmds set <i> <routine> <hex phrase> <hex phonemes>" (i = the count adds one), "cmds del <i>" -> the list again.
	 */
	static void printCommands(){
		std::string all;
		{
			std::lock_guard<std::mutex> g(UserData::lock);
			for(const auto& c : UserData::commands){
				if(!all.empty()) all += ',';
				all += std::to_string(c.routine) + ":" + toHex(c.phrase) + ":" + toHex(c.phonemes);
			}
		}
		printf("[BBCMDS] max=%u d=%s\n", (unsigned) UserData::MaxCommands, all.empty() ? "-" : all.c_str());
	}

	static void commandsCommand(const std::string& args){
		bool ok = false;
		int idx = -1, routine = -1;
		char ph[100] = {}, pn[140] = {};
		if(sscanf(args.c_str(), "del %d", &idx) == 1 && idx >= 0){
			ok = UserData::deleteCommand((size_t) idx);
		}else if(sscanf(args.c_str(), "set %d %d %99s %139s", &idx, &routine, ph, pn) == 4 && idx >= 0 && routine >= 0){
			ok = UserData::setCommand((size_t) idx, (uint8_t) routine, fromHex(ph, strlen(ph)), fromHex(pn, strlen(pn)));
		}
		if(!ok) printf("[BBCMDS] result=error\n");
		printCommands();
	}

	/*
	 * Fork: schedules and quiet hours. "sched" -> [BBSCHED] max=<n> d=<days>:<minute>:<routine>:<on>,... ("-")
	 * quiet=<on>:<start minute>:<end minute>. "sched set <i> <days> <minute> <routine> <on>", "sched del <i>",
	 * "quiet <on> <start minute> <end minute>" -> the same line again. days: bit 0 = Sunday .. bit 6 = Saturday.
	 */
	static void printSchedules(){
		std::string all;
		bool qOn;
		unsigned qs, qe;
		{
			std::lock_guard<std::mutex> g(UserData::lock);
			for(const auto& sc : UserData::schedules){
				if(!sc.used) continue;
				if(!all.empty()) all += ',';
				all += std::to_string(sc.days) + ":" + std::to_string(sc.minute) + ":" + std::to_string(sc.routine) + ":" + (sc.on ? "1" : "0");
			}
			qOn = UserData::quietOn;
			qs = UserData::quietStart;
			qe = UserData::quietEnd;
		}
		printf("[BBSCHED] max=%u d=%s quiet=%d:%u:%u\n", (unsigned) UserData::MaxSchedules, all.empty() ? "-" : all.c_str(),
		       qOn ? 1 : 0, qs, qe);
	}

	static void schedulesCommand(const std::string& args){
		unsigned i, days, minute, routine, on, start, end;
		bool ok = false;
		if(sscanf(args.c_str(), "sched set %u %u %u %u %u", &i, &days, &minute, &routine, &on) == 5){
			ok = UserData::setSchedule(i, (uint8_t) days, (uint16_t) minute, (uint8_t) routine, on != 0);
		}else if(sscanf(args.c_str(), "sched del %u", &i) == 1){
			ok = UserData::deleteSchedule(i);
		}else if(sscanf(args.c_str(), "quiet %u %u %u", &on, &start, &end) == 3 && start < 24 * 60 && end < 24 * 60){
			ok = UserData::setQuiet(on != 0, (uint16_t) start, (uint16_t) end);
		}
		if(!ok) printf("[BBSCHED] result=error\n");
		printSchedules();
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

	/*
	 * Fork, dev aid: "render <hex text>" (the current voice) or
	 * "render <pitch> <range> <stretch> <fx 0-3> <warbleHz> <depth> <hpHz> <lpHz> <gain> <hex text>" synthesizes the
	 * text through the full voice chain (speech + effect + reverb) without playing it, and sends the 16 kHz mono PCM
	 * to the PC: [BBREND] n=<samples> sr=16000 lines=<k>, k x [BBREND] i=<line> d=<base64>, [BBREND] done.
	 * Lets Dimension Hopper's dev scripts compare the robot's voice with a reference recording.
	 */
	void render(const std::string& args){
		// newlib nano's sscanf has no %f: parse the numbers with strtof
		float v[9];
		const char* cur = args.c_str();
		bool custom = true;
		for(float& f : v){
			char* end = nullptr;
			f = strtof(cur, &end);
			if(end == cur || *end != ' '){
				custom = false;
				break;
			}
			cur = end + 1;
		}
		const char* hex = custom ? cur : args.c_str();
		const std::string text = fromHex(hex, strlen(hex));
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		Audio* audio = getService<Audio>();
		SpeechAudioGen* gen = ServiceLocator::SpeechAudioGenInstance.get();
		if(text.empty() || audio == nullptr || gen == nullptr || !ServiceLocator::SpeechGenInstance){
			printf("[BBREND] result=error\n");
			return;
		}
		if(state == nullptr || strcmp(state->name(), "idle") != 0 || audio->isPlaying()){
			printf("[BBREND] result=busy\n");
			return;
		}
		if(custom){
			Voice::overrideVals = { v[0], v[1], v[2], static_cast<Voice::Fx>(std::clamp((int) v[3], 0, 3)),
			                        { v[4], v[5], v[6], v[7], v[8] } };
			Voice::overrideOn = true;
		}
		static constexpr size_t MaxSamples = 16000 * 8;
		std::vector<int16_t> pcm;
		pcm.reserve(MaxSamples);
		gen->open(std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text));
		std::array<int16_t, 512> chunk{};
		while(pcm.size() < MaxSamples){
			const size_t got = gen->getData(reinterpret_cast<uint8_t*>(chunk.data()), sizeof(chunk)) / sizeof(int16_t);
			if(got == 0) break;
			pcm.insert(pcm.end(), chunk.begin(), chunk.begin() + std::min(got, MaxSamples - pcm.size()));
		}
		gen->close();
		Voice::overrideOn = false;

		static constexpr size_t LineBytes = 1500;
		const size_t bytes = pcm.size() * sizeof(int16_t);
		const size_t lines = (bytes + LineBytes - 1) / LineBytes;
		printf("[BBREND] n=%u sr=16000 lines=%u\n", (unsigned) pcm.size(), (unsigned) lines);
		std::vector<unsigned char> b64(LineBytes * 4 / 3 + 8);
		const auto* raw = reinterpret_cast<const unsigned char*>(pcm.data());
		for(size_t i = 0; i < lines; ++i){
			const size_t len = std::min(LineBytes, bytes - i * LineBytes);
			size_t outLen = 0;
			mbedtls_base64_encode(b64.data(), b64.size(), &outLen, raw + i * LineBytes, len);
			printf("[BBREND] i=%u d=%.*s\n", (unsigned) i, (int) outLen, (const char*) b64.data());
			EventLog::syncConsole(); // wait until the UART has sent it: pushing ~70 KB at once drops bytes
			vTaskDelay(pdMS_TO_TICKS(10));
		}
		printf("[BBREND] done\n");
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

	/** Fork: the controller's Robot status page asked (Ctrl::Status). */
	void sendRobotStatus(){
		Com* com = getService<Com>();
		if(com == nullptr || !com->controllerPresent()) return;
		const Battery* battery = getService<Battery>();
		const HomeWiFi::Config c = HomeWiFi::load();
		auto kb = [](size_t bytes){ return (uint8_t) std::min<size_t>(bytes / 1024, 255); };

		RobotStatusData st;
		if(ServiceLocator::SettingsInstance){
			const SettingsStruct s = ServiceLocator::SettingsInstance->get();
			st.frontSensor = (uint8_t) s.frontSensor;
			st.bottomSensor = (uint8_t) s.bottomSensor;
			st.activity = (uint8_t) s.activityMode;
		}
		if(battery != nullptr){
			st.battery = (uint8_t) std::clamp(battery->getPerc() + 0.5f, 0.0f, 100.0f);
			switch(battery->getChargingState()){
				case ChargingState::Unplugged: st.charge = 0; break;
				case ChargingState::Charging: st.charge = 1; break;
				case ChargingState::Full: st.charge = 2; break;
			}
		}
		st.ramKb = kb(heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
		st.ramMinKb = (uint8_t) std::min<uint32_t>(MemMark::lowestKb(), 255);
		st.wifi = c.ssid.empty() ? 0 : 1;
		st.ai = c.aiEnabled ? 1 : 0;
		st.brain = c.brainIp != 0 ? 1 : 0;
		st.upMin = (uint16_t) std::min<uint64_t>(millis() / 60000, 65535);
		st.fw = esp_app_get_description()->version;
		st.network = c.ssid;
		st.problem = HomeWiFi::lastProblem();
		ListenState::lastConversation(st.heard, st.said);
		com->sendData(BB::State::Idle, BB::Action::Idle::RobotStatus, st);
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
		printf("[BBWIFI] configured=%d ssid_hex=%s ai=%s brain=%s last=%s\n", c.ssid.empty() ? 0 : 1,
		       ssidHex.empty() ? "-" : ssidHex.c_str(), c.aiEnabled ? "on" : "off", brain, HomeWiFi::lastProblem());
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
			const HomeWiFi::Result r = wifi.start(HomeWiFi::load(), 10000);
			if(r != HomeWiFi::Result::Ok){
				printf("[BBWIFI] result=failed why=%s reason=%u err=%d joined=%d ram=%u\n", HomeWiFi::resultName(r),
				       (unsigned) wifi.reason(), wifi.startError(), wifi.wasAssociated() ? 1 : 0,
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
				       (unsigned) MemMark::lowestKb(), brainText);
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
		int radioMode; // 0 balance, 1 wifi, 2 quiet
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
		auto* ba = new BenchArgs{ a.addr, static_cast<uint16_t>(port), kb, strcmp(coex, "quiet") == 0 ? 2 : (strcmp(coex, "wifi") == 0 ? 1 : 0), this };
		xTaskCreatePinnedToCore([](void* arg){
			auto* ba = static_cast<BenchArgs*>(arg);
			{
				HomeWiFi wifi;
				const HomeWiFi::Result r = wifi.start(HomeWiFi::load(), 8000);
				if(r != HomeWiFi::Result::Ok){
					printf("[BBWIFI] result=failed why=%s\n", HomeWiFi::resultName(r));
				}else{
					const int64_t ms = BrainLink::uploadTest(ba->ip, ba->port, ba->kb * 1024, ba->radioMode);
					const unsigned ram = (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024);
					const unsigned ramMin = (unsigned) MemMark::lowestKb();
					if(ms < 0){
						printf("[BBWIFI] result=bench_failed kb=%lu coex=%s rssi=%d ram=%u\n", (unsigned long) ba->kb,
						       (ba->radioMode == 2 ? "quiet" : ba->radioMode == 1 ? "wifi" : "balance"), wifi.rssi(), ram);
					}else{
						printf("[BBWIFI] result=bench kb=%lu ms=%lld kbps=%lld coex=%s rssi=%d ram=%u ram_min=%u\n",
						       (unsigned long) ba->kb, ms, ms > 0 ? (int64_t) ba->kb * 1000 / ms : 0,
						       (ba->radioMode == 2 ? "quiet" : ba->radioMode == 1 ? "wifi" : "balance"), wifi.rssi(), ram, ramMin);
					}
				}
				wifi.stop();
			}
			ba->bot->wifiTesting = false;
			delete ba;
			vTaskDelete(nullptr);
		}, "bbwifibench", 6144, ba, 3, nullptr, 0);
	}

	/*
	 * Diagnostics: "srtest [wifi] [dur=<ms>]" loads the full offline recognizer the way a listen does and reports the
	 * lowest internal RAM reached during the load. "wifi" runs a WiFi session first (like an AI listen would);
	 * "dur" tries another MultiNet duration. One line: [BBSR] start= after_wifi= low_load= after_load= after_unload=
	 */
	struct SrTestArgs {
		bool wifi;
		int32_t dur;
		Butterbot* bot;
	};

	void srTest(const char* args){
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		if(state == nullptr || strcmp(state->name(), "idle") != 0){
			printf("[BBSR] result=busy state=%s\n", state != nullptr ? state->name() : "none");
			return;
		}
		if(wifiTesting.exchange(true)){
			printf("[BBSR] result=busy state=testing\n");
			return;
		}
		int dur = 0;
		if(const char* d = strstr(args, "dur=")) dur = atoi(d + 4);
		auto* sa = new SrTestArgs{ strstr(args, "wifi") != nullptr, dur, this };
		xTaskCreatePinnedToCore([](void* arg){
			auto* sa = static_cast<SrTestArgs*>(arg);
			auto kb = [](size_t b){ return (unsigned) (b / 1024); };
			const size_t start = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
			size_t afterWifi = start, largestAfterWifi = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
			if(sa->wifi){
				HomeWiFi wifi;
				wifi.start(HomeWiFi::load(), 8000);
				wifi.stop();
				vTaskDelay(pdMS_TO_TICKS(300)); // let deleted tasks be cleaned up
				afterWifi = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
				largestAfterWifi = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
			}
			AudioFrontend* afe = sa->bot->getService<AudioFrontend>();
			if(afe == nullptr){
				printf("[BBSR] result=error why=no_afe\n");
			}else{
				AudioFrontend::diagMultinetDuration = sa->dur;
				heap_caps_monitor_local_minimum_free_size_start();
				afe->setMode(AudioFrontend::Mode::Off);
				afe->loadModel(true);
				const size_t lowLoad = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
				const size_t afterLoad = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
				const size_t largestLoaded = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
				afe->unloadModel();
				heap_caps_monitor_local_minimum_free_size_stop();
				AudioFrontend::diagMultinetDuration = 0;
				vTaskDelay(pdMS_TO_TICKS(300));
				printf("[BBSR] result=ok wifi=%d dur=%ld start=%u after_wifi=%u largest_after_wifi=%u low_load=%u "
				       "after_load=%u largest_loaded=%u after_unload=%u\n", sa->wifi ? 1 : 0,
				       (long) (sa->dur > 0 ? sa->dur : 6000), kb(start), kb(afterWifi), kb(largestAfterWifi), kb(lowLoad),
				       kb(afterLoad), kb(largestLoaded), kb(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)));
			}
			sa->bot->wifiTesting = false;
			delete sa;
			vTaskDelete(nullptr);
		}, "bbsrtest", 6144, sa, 3, nullptr, 0);
	}

	/*
	 * Fork: "cam" - one camera frame for Dimension Hopper's Diagnostics tab. Replies with one line:
	 * [BBCAM] w=<px> h=<px> n=<jpeg bytes> jpg=<base64 JPEG>   or   [BBCAM] result=busy why=<state|routine|facedet|working>
	 * Only while idle with no routine running: face recognition, "what is this?" and RC video use the camera too.
	 * The app asks again after each frame, so the frame rate is whatever the USB link manages (~1-2 fps).
	 */
	static constexpr int CamJpegQuality = 40;
	std::atomic<bool> camWorking{ false };

	void camFrame(){
		const BBStateMachine* sm = getService<BBStateMachine>();
		const State* state = sm != nullptr ? sm->getActiveState() : nullptr;
		const char* busy = nullptr;
		if(state == nullptr || strcmp(state->name(), "idle") != 0) busy = "state";
		else if(sm->getActiveRoutine() != nullptr) busy = "routine";
		else if(const FaceDet* fd = getService<FaceDet>(); fd != nullptr && fd->isStarted()) busy = "facedet";
		else if(getDevice<Camera>() == nullptr) busy = "no_camera";
		if(busy){
			printf("[BBCAM] result=busy why=%s\n", busy);
			return;
		}
		if(camWorking.exchange(true)){
			printf("[BBCAM] result=busy why=working\n");
			return;
		}
		// One worker, created the first time (JPEG encoding needs more stack than the console task has; a PSRAM stack
		// is fine since nothing here touches flash). It waits for requests instead of being deleted after each frame.
		if(camTask == nullptr){
			const BaseType_t ok = xTaskCreatePinnedToCoreWithCaps([](void* self){
				auto* bot = static_cast<Butterbot*>(self);
				for(;;){
					ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
					bot->sendCamFrame();
					bot->camWorking = false;
				}
			}, "bbcam", 8192, this, 2, &camTask, tskNO_AFFINITY, MALLOC_CAP_SPIRAM);
			if(ok != pdPASS){
				camTask = nullptr;
				camWorking = false;
				printf("[BBCAM] result=error why=task\n");
				return;
			}
		}
		xTaskNotifyGive(camTask);
	}

	TaskHandle_t camTask = nullptr;

	void sendCamFrame(){
		Camera* cam = getDevice<Camera>();
		camera_fb_t* fb = cam->getFrame();
		uint8_t* jpg = nullptr;
		size_t jpgLen = 0;
		uint16_t w = 0, h = 0;
		if(fb != nullptr && fb->buf != nullptr && fb->len > 0){
			w = fb->width;
			h = fb->height;
			if(!frame2jpg(fb, CamJpegQuality, &jpg, &jpgLen)) jpg = nullptr;
		}
		cam->releaseFrame();
		if(jpg == nullptr){
			printf("[BBCAM] result=error why=capture\n");
			return;
		}
		static constexpr char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
		const size_t outLen = (jpgLen + 2) / 3 * 4;
		char* out = static_cast<char*>(heap_caps_malloc(outLen + 1, MALLOC_CAP_SPIRAM));
		if(out == nullptr){
			free(jpg);
			printf("[BBCAM] result=error why=memory\n");
			return;
		}
		size_t o = 0;
		for(size_t i = 0; i < jpgLen; i += 3){
			const uint32_t a = jpg[i], b = i + 1 < jpgLen ? jpg[i + 1] : 0, c = i + 2 < jpgLen ? jpg[i + 2] : 0;
			const uint32_t v = (a << 16) | (b << 8) | c;
			out[o++] = B64[(v >> 18) & 63];
			out[o++] = B64[(v >> 12) & 63];
			out[o++] = i + 1 < jpgLen ? B64[(v >> 6) & 63] : '=';
			out[o++] = i + 2 < jpgLen ? B64[v & 63] : '=';
		}
		out[o] = 0;
		printf("[BBCAM] w=%u h=%u n=%u jpg=%s\n", (unsigned) w, (unsigned) h, (unsigned) jpgLen, out);
		free(out);
		free(jpg);
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

	static void speakTextAndWait(Audio* audio, const std::string& text) {
		if(audio == nullptr || !ServiceLocator::SpeechAudioGenInstance || !ServiceLocator::SpeechGenInstance || text.empty()){
			return;
		}
		audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text));
		audio->waitEnd(portMAX_DELAY);
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
		// Fork: conversation mode ends with any controller button (the states see the same command and stop
		// listening). The press that ends it does nothing else - Shut Up doesn't also toggle mute.
		if(Conversation::active() && command != Ctrl::Status && command != Ctrl::Converse && command != Ctrl::Drive &&
		   command != Ctrl::Settings){
			Conversation::end("button");
			return;
		}
		if(command == Ctrl::Status){
			// Reads the WiFi settings from flash, so it runs on an internal-stack task
			runFlashJob([this]{ sendRobotStatus(); });
			return;
		}
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
			Conversation::end("button"); // fork: the robot's button ends conversation mode too
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
