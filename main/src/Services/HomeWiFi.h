#ifndef BUTTERBOT_FIRMWARE_HOMEWIFI_H
#define BUTTERBOT_FIRMWARE_HOMEWIFI_H

#include <cstdint>
#include <string>
#include <vector>
#include <esp_netif.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>

/**
 * Fork: joining the owner's home WiFi, for the AI "brain" running on their PC (Dimension Hopper).
 *
 * The robot's other WiFi use (RC mode) is the controller's private access point, handled by CMF's WiFi class
 * with a fixed IP on channel 1 - not usable for a home network. This is a small, separate station with DHCP.
 * It's only switched on for a conversation (ListenState) or a test, and switched off afterwards, because WiFi
 * needs internal RAM that Bluetooth and the speech front end also live on. Never at the same time as RC mode.
 *
 * Settings (NVS "bbai"): ssid, pass, enabled flag, last brain address. Set over USB by Dimension Hopper.
 */
class HomeWiFi {
public:
	struct Config {
		std::string ssid;
		std::string pass;
		bool aiEnabled = false;
		uint32_t brainIp = 0;   // network byte order, 0 = unknown
		uint16_t brainPort = 0;
	};

	static Config load();
	static void saveCredentials(const std::string& ssid, const std::string& pass);
	static void saveAiEnabled(bool on);
	static void saveBrain(uint32_t ip, uint16_t port);
	static void clear();

	enum class Result : uint8_t { Ok, NotConfigured, NotFound, WrongPassword, Timeout, Failed };
	static const char* resultName(Result r);

	/** Starts WiFi and blocks until it has an IP address (or fails/times out). Call stop() afterwards either way. */
	Result start(const Config& cfg, uint32_t timeoutMs);
	void stop();

	/** Networks the robot can see (2.4 GHz - all it can use), strongest first, one entry per name. Call stop() after. */
	struct Network {
		std::string ssid;
		int rssi;
		bool open;
	};
	bool scan(std::vector<Network>& out);

	bool isUp() const { return up; }
	uint32_t ip() const { return ipAddr; }
	int rssi() const;

private:
	bool bringUp();
	esp_netif_t* netif = nullptr;
	EventGroupHandle_t events = nullptr;
	esp_event_handler_instance_t wifiHandler = nullptr;
	esp_event_handler_instance_t ipHandler = nullptr;
	bool started = false;
	bool loopCreated = false;
	bool up = false;
	uint32_t ipAddr = 0;
	volatile uint8_t lastReason = 0;

	static constexpr EventBits_t GotIp = BIT0;
	static constexpr EventBits_t Failed = BIT1;
};

#endif //BUTTERBOT_FIRMWARE_HOMEWIFI_H
