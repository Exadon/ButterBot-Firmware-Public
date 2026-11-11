#include "BaseBoard.h"
#include <algorithm>
#include "driver/gpio.h"
#include "Pins.hpp"
#include <Util/stdafx.h>
#include <Util/ServiceLocator.h>

DEFINE_LOG(BaseBoard)

/**
 * Runtime I2C errors (a noisy bus, a brown-out on the base MCU while motors spin up) used to go
 * through ESP_ERROR_CHECK, which aborts and reboots the whole robot. Log and carry on instead -
 * the motor dead-man switch on the base board stops the motors if writes keep failing.
 */
static bool checkI2C(esp_err_t err, const char* what){
	if(err == ESP_OK){
		return true;
	}

	CMF_LOG(BaseBoard, LogLevel::Error, "I2C op failed (%s): %s", what, esp_err_to_name(err));
	return false;
}

#define BB_I2C_CHECK(x) checkI2C((x), #x)

SemaphoreHandle_t BaseBoard::sem;
std::atomic<bool> BaseBoard::proxRebaselineDue{ false };

BaseBoard::BaseBoard(std::unique_ptr<I2CDevice> device, bool internalStack) : AsyncEntity(0, 3 * 1024, 7, -1, internalStack), dev(std::move(device)){
	CMF_LOG(BaseBoard, LogLevel::Info, "Initializing BaseBoard");
	if(!dev){
		CMF_LOG(BaseBoard, LogLevel::Error, "I2C device is null");
		abort();
	}

	uint8_t id = 0;
	if(dev->readRegister(Identify, id) != ESP_OK){
		CMF_LOG(BaseBoard, LogLevel::Error, "Transmission error");
		abort();
	}

	if(id != I2CAddress){
		// Previously returned here, leaving sem and the timers null so the first tick()/setMotors() crashed.
		CMF_LOG(BaseBoard, LogLevel::Error, "ID mismatch: expected %d, got %d - continuing anyway", I2CAddress, id);
	}

	sem = xSemaphoreCreateBinary();
	motorTimer = newObject<Timer>(this, MotorControlPeriod, timerFunc, nullptr, "baseBoard");
	proxTimer = newObject<Timer>(this, ProxRebaselineDelayMs, proxTimerFunc, nullptr, "baseBoardProx");

	reset();

	gpio_isr_handler_add((gpio_num_t)MOTOR_BRD_INT, motorBoardISR, &sem);
	gpio_config_t io_conf = {
		.pin_bit_mask = 1ULL << MOTOR_BRD_INT,
		.mode = GPIO_MODE_INPUT,
		.pull_up_en = GPIO_PULLUP_DISABLE,
		.pull_down_en = GPIO_PULLDOWN_ENABLE,
		.intr_type = GPIO_INTR_HIGH_LEVEL
	};
	gpio_config(&io_conf);
}

void BaseBoard::setMotors(int8_t left, int8_t right){
	left = -left;
	right = -right;

	regStateMutex.lock();
	if(left == registerState.motorLeft && right == registerState.motorRight){
		regStateMutex.unlock();
		return;
	}
	motorTimer->stop();
	registerState.motorLeft = left;
	registerState.motorRight = right;
	const bool scanning = registerState.proximityScanning && !isBottomSensorOn();
	regStateMutex.unlock();

	if(scanning){
		BB_I2C_CHECK(dev->writeRegister(ProximityScanning, static_cast<uint8_t>(0)));
		delayMillis(ProxStopSettleMs);
	}

	//Writing to both registers sequentially
	int8_t arr[] = { left, right };
	BB_I2C_CHECK(dev->writeRegister(MotorL, (uint8_t*)&arr, 2));

	if(scanning){
		proxTimer->reset();
	}

	updateMotorTimer();
}

void BaseBoard::setMotorLeft(int8_t value){
	value = -value;

	regStateMutex.lock();
	if(value == registerState.motorLeft){
		regStateMutex.unlock();
		return;
	}
	motorTimer->stop();
	registerState.motorLeft = value;
	const bool scanning = registerState.proximityScanning && !isBottomSensorOn();
	regStateMutex.unlock();

	if(scanning){
		BB_I2C_CHECK(dev->writeRegister(ProximityScanning, static_cast<uint8_t>(0)));
		delayMillis(ProxStopSettleMs);
	}

	BB_I2C_CHECK(dev->writeRegister(MotorL, (uint8_t*)&value, 1));

	if(scanning){
		proxTimer->reset();
	}

	updateMotorTimer();
}

void BaseBoard::setMotorRight(int8_t value){
	value = -value;

	regStateMutex.lock();
	if(value == registerState.motorRight){
		regStateMutex.unlock();
		return;
	}
	motorTimer->stop();
	registerState.motorRight = value;
	const bool scanning = registerState.proximityScanning && !isBottomSensorOn();
	regStateMutex.unlock();

	if(scanning){
		BB_I2C_CHECK(dev->writeRegister(ProximityScanning, static_cast<uint8_t>(0)));
		delayMillis(ProxStopSettleMs);
	}

	BB_I2C_CHECK(dev->writeRegister(MotorR, (uint8_t*)&value, 1));

	if(scanning){
		proxTimer->reset();
	}

	updateMotorTimer();
}

void BaseBoard::stopMotors(){
	setMotors(0, 0);
}

void BaseBoard::setProximityScanning(bool enabled){
	regStateMutex.lock();
	const bool changed = enabled != registerState.proximityScanning;
	registerState.proximityScanning = enabled;
	regStateMutex.unlock();

	if(!changed){
		return;
	}

	BB_I2C_CHECK(dev->writeRegister(ProximityScanning, (uint8_t*)&enabled, 1));
}

void BaseBoard::rebaselineProximity(){
	regStateMutex.lock();
	const bool scanning = registerState.proximityScanning;
	regStateMutex.unlock();

	if(!scanning){
		return;
	}

	BB_I2C_CHECK(dev->writeRegister(ProximityScanning, static_cast<uint8_t>(0)));
	delayMillis(ProxStopSettleMs);
	BB_I2C_CHECK(dev->writeRegister(ProximityScanning, static_cast<uint8_t>(1)));
}

// Absolute threshold I2C commands removed — Base uses relative band logic now
void BaseBoard::setProximityFrontThreshold([[maybe_unused]] uint16_t threshold){
	// registerState.proximityFrontThreshold = threshold;
	// BB_I2C_CHECK(dev->writeRegister(ProximityFrontThresh, (uint8_t*)&threshold, 2));
}

void BaseBoard::setProximityBotThreshold([[maybe_unused]] uint16_t threshold){
	// registerState.proximityBottomThreshold = threshold;
	// BB_I2C_CHECK(dev->writeRegister(ProximityBottomThresh, (uint8_t*)&threshold, 2));
}

void BaseBoard::requestProximityState(){
	BB_I2C_CHECK(dev->write(ProximityStateRequest));
}

ChargingState BaseBoard::getChargingState(){
	uint8_t state = 0;
	esp_err_t err = dev->readRegister(ChargeState, state);
	if(err != ESP_OK){
		CMF_LOG(BaseBoard, LogLevel::Error, "ChargeState read failed: %s", esp_err_to_name(err));
		return ChargingState::Unplugged;
	}

	if(state > 2){
		CMF_LOG(BaseBoard, LogLevel::Error, "Invalid chargeState value: %u", state);
		return ChargingState::Unplugged;
	}

	return static_cast<ChargingState>(state);
}

void BaseBoard::tick(float deltaTime) noexcept{
	while(xSemaphoreTake(sem, portMAX_DELAY) != pdTRUE){
		vTaskDelay(1);
	}

	regStateMutex.lock();
	if(registerState.motorLeft != 0 || registerState.motorRight != 0){
		//Writing to both registers sequentially
		int8_t arr[] = { registerState.motorLeft, registerState.motorRight };
		BB_I2C_CHECK(dev->writeRegister(MotorL, (uint8_t*)&arr, 2));
		updateMotorTimer();
	}
	regStateMutex.unlock();

	if(proxRebaselineDue.exchange(false)){
		rebaselineProximity();
	}

	if(gpio_get_level((gpio_num_t)MOTOR_BRD_INT) == 0){
		//No events to process
		gpio_intr_enable((gpio_num_t) MOTOR_BRD_INT);
		return;
	}

	uint8_t numEvents = 0;
	if(!BB_I2C_CHECK(dev->readRegister(EventCount, numEvents))){
		numEvents = 0;
	}

	std::queue<EventData> queue;
	// Read in bounded chunks: the old code put numEvents (up to 255) EventData on this task's 3 KB stack,
	// and looped forever if the base board kept reporting events while a read failed.
	static constexpr uint8_t MaxEventsPerRead = 8;
	static constexpr uint8_t MaxReadRounds = 16;
	for(uint8_t pass = 0; numEvents > 0 && pass < MaxReadRounds; ++pass){
		const uint8_t toRead = std::min(numEvents, MaxEventsPerRead);
		EventData rawData[MaxEventsPerRead];
		uint8_t wData[2] = { EventsGet, toRead };
		if(!BB_I2C_CHECK(dev->write_read(wData, 2, (uint8_t*) rawData, sizeof(EventData) * toRead))){
			break;
		}

		for(uint8_t i = 0; i < toRead; i++){
			queue.emplace(rawData[i]);
		}

		if(!BB_I2C_CHECK(dev->readRegister(EventCount, numEvents))){
			break;
		}
	}

	gpio_intr_enable((gpio_num_t) MOTOR_BRD_INT);

	while(!queue.empty()){
		processEventData(queue.front());
		queue.pop();
	}
}

void BaseBoard::reset(){
	motorTimer->stop();

	BB_I2C_CHECK(dev->write(Reset));
	registerState = {};

	delayMillis(ResetDelay);
}

void BaseBoard::updateMotorTimer(){
	if(registerState.motorLeft == 0 && registerState.motorRight == 0){
		motorTimer->stop();
	} else{
		motorTimer->reset();
	}
}

uint8_t BaseBoard::getEventCount(){
	uint8_t numEvents = 0;
	BB_I2C_CHECK(dev->readRegister(EventCount, numEvents));
	return numEvents;
}

void BaseBoard::timerFunc(void*){
	xSemaphoreGive(sem);
}

void BaseBoard::proxTimerFunc(void*){
	proxRebaselineDue = true;
	xSemaphoreGive(sem);
}

void IRAM_ATTR BaseBoard::motorBoardISR(void* arg){
	gpio_intr_disable((gpio_num_t)MOTOR_BRD_INT);
	auto sem = (SemaphoreHandle_t*)arg;
	auto higherPrioTask = pdFALSE;
	xSemaphoreGiveFromISR(*sem, &higherPrioTask);
	portYIELD_FROM_ISR(higherPrioTask);
}

bool BaseBoard::isFrontSensorOff() const{
	if(!ServiceLocator::SettingsInstance){
		return false;
	}

	return ServiceLocator::SettingsInstance->get().frontSensor == FrontSensorMode::Off;
}

bool BaseBoard::isBottomSensorOff() const{
	if(!ServiceLocator::SettingsInstance){
		return false;
	}

	return ServiceLocator::SettingsInstance->get().bottomSensor == BottomSensorMode::Off;
}

bool BaseBoard::isBottomSensorOn() const{
	if(!ServiceLocator::SettingsInstance){
		return false;
	}

	return ServiceLocator::SettingsInstance->get().bottomSensor == BottomSensorMode::On;
}

void BaseBoard::processProximityReading(ProxState front, ProxState bot){
	if(isFrontSensorOff()){
		front = ProxState::Uncovered;
	}

	if(isBottomSensorOff()){
		bot = ProxState::Covered;
	}

	onProximityReading.broadcast(front, bot);
}

void BaseBoard::processProximityChange(ProxSensor sensor, bool inThreshold){
	if(sensor == ProxSensor::Front && isFrontSensorOff()){
		return;
	}

	if(sensor == ProxSensor::Bottom && isBottomSensorOff()){
		return;
	}

	printf("[BaseBoard][%llu] %s sensor %s\n", millis(), sensor == ProxSensor::Front ? "Front" : "Bottom", inThreshold ? "covered" : "uncovered");
	onProximityChange.broadcast(sensor, inThreshold);
}

void BaseBoard::processEventData(const EventData& eventData){
	switch(eventData.type){
		case EventType::ProximityReading:
			processProximityReading(eventData.data.proxReading.front, eventData.data.proxReading.bot);
			break;
		case EventType::ProximityThresholdReached:
			processProximityChange(eventData.data.proxThresholdReached.sensor, eventData.data.proxThresholdReached.inThreshold);
			break;
		case EventType::ChargeStateChanged:
			onChargeChanged.broadcast(eventData.data.chargeStateChanged);
			break;
		default:
			CMF_LOG(BaseBoard, LogLevel::Error, "Invalid event code %d", eventData.type);
			return;
	}
}
