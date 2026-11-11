#include "Settings.h"
#include <nvs_flash.h>
#include <algorithm>
#include <cstring>
#include <vector>

DEFINE_LOG(Settings)

Settings::Settings(){
	ESP_ERROR_CHECK(nvs_open(NVSNamespace, NVS_READWRITE, &handle));
	load();
}

Settings::~Settings(){
	nvs_close(handle);
}

SettingsStruct Settings::get() const{
	return settingsStruct;
}

void Settings::set(const SettingsStruct& settings){
	settingsStruct = settings;
}

void Settings::store() const{
	esp_err_t err = nvs_set_blob(handle, BlobName, &settingsStruct, sizeof(SettingsStruct));
	if(err != ESP_OK){
		CMF_LOG(Settings, LogLevel::Error, "Error storing settings: %s", esp_err_to_name(err));
		return;
	}
	nvs_commit(handle);
}

void Settings::load(){
	settingsStruct = SettingsStruct();

	// The blob is a raw SettingsStruct, so its stored size changes whenever a field is added.
	// nvs_get_blob() fails outright on a size mismatch, which used to reset every setting to its default
	// after a firmware update. Read whatever is stored and copy the common prefix instead: fields that
	// already existed keep their values, new (appended) fields keep their defaults.
	// Rule for the fork: only ever APPEND fields to SettingsStruct.
	size_t storedLen = 0;
	esp_err_t err = nvs_get_blob(handle, BlobName, nullptr, &storedLen);
	if(err != ESP_OK || storedLen == 0){
		CMF_LOG(Settings, LogLevel::Warning, "No stored settings found, using defaults: %s", esp_err_to_name(err));
	}else{
		std::vector<uint8_t> stored(storedLen);
		err = nvs_get_blob(handle, BlobName, stored.data(), &storedLen);
		if(err != ESP_OK){
			CMF_LOG(Settings, LogLevel::Warning, "Stored settings unreadable, using defaults: %s", esp_err_to_name(err));
		}else{
			if(storedLen != sizeof(SettingsStruct)){
				CMF_LOG(Settings, LogLevel::Info, "Migrating settings blob (%zu -> %zu bytes)", storedLen, sizeof(SettingsStruct));
			}
			memcpy(&settingsStruct, stored.data(), std::min(storedLen, sizeof(SettingsStruct)));
		}
	}

	switch(settingsStruct.tempHumScale){
		case SettingsStruct::Scale::Celsius:
		case SettingsStruct::Scale::Fahrenheit:
		case SettingsStruct::Scale::Kelvin:
			break;
		default:
			settingsStruct.tempHumScale = SettingsStruct().tempHumScale;
			break;
	}

	switch(settingsStruct.frontSensor){
		case FrontSensorMode::Off:
		case FrontSensorMode::On:
			break;
		default:
			settingsStruct.frontSensor = SettingsStruct().frontSensor;
			break;
	}

	switch(settingsStruct.bottomSensor){
		case BottomSensorMode::Off:
		case BottomSensorMode::Reduced:
		case BottomSensorMode::On:
			break;
		default:
			settingsStruct.bottomSensor = SettingsStruct().bottomSensor;
			break;
	}

	switch(settingsStruct.activityMode){
		case ActivityMode::Off:
		case ActivityMode::Chill:
		case ActivityMode::Active:
		case ActivityMode::Hyper:
			break;
		default:
			settingsStruct.activityMode = SettingsStruct().activityMode;
			break;
	}
}