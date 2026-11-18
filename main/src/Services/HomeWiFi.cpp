#include "HomeWiFi.h"
#include <algorithm>
#include <cstring>
#include <esp_event.h>
#include <esp_wifi.h>
#include <esp_heap_caps.h>
#include <nvs.h>
#include "Util/MemMark.h"
#include <esp_coexist.h>
#include "BLE/ConMan.h"
#include "Log/Log.h"

DEFINE_LOG(HomeWiFi)

namespace {
	constexpr const char* Ns = "bbai";

	std::string getStr(nvs_handle_t h, const char* key){
		size_t len = 0;
		if(nvs_get_str(h, key, nullptr, &len) != ESP_OK || len == 0) return {};
		std::string s(len, '\0');
		nvs_get_str(h, key, s.data(), &len);
		s.resize(len > 0 ? len - 1 : 0);
		return s;
	}
}

HomeWiFi::Config HomeWiFi::load(){
	Config c;
	nvs_handle_t h;
	if(nvs_open(Ns, NVS_READONLY, &h) != ESP_OK) return c;
	c.ssid = getStr(h, "ssid");
	c.pass = getStr(h, "pass");
	uint8_t on = 0;
	nvs_get_u8(h, "ai", &on);
	c.aiEnabled = on != 0;
	nvs_get_u32(h, "bip", &c.brainIp);
	nvs_get_u16(h, "bport", &c.brainPort);
	nvs_close(h);
	return c;
}

void HomeWiFi::saveCredentials(const std::string& ssid, const std::string& pass){
	nvs_handle_t h;
	if(nvs_open(Ns, NVS_READWRITE, &h) != ESP_OK) return;
	nvs_set_str(h, "ssid", ssid.c_str());
	nvs_set_str(h, "pass", pass.c_str());
	nvs_commit(h);
	nvs_close(h);
}

void HomeWiFi::saveAiEnabled(bool on){
	nvs_handle_t h;
	if(nvs_open(Ns, NVS_READWRITE, &h) != ESP_OK) return;
	nvs_set_u8(h, "ai", on ? 1 : 0);
	nvs_commit(h);
	nvs_close(h);
}

void HomeWiFi::saveBrain(uint32_t ip, uint16_t port){
	nvs_handle_t h;
	if(nvs_open(Ns, NVS_READWRITE, &h) != ESP_OK) return;
	nvs_set_u32(h, "bip", ip);
	nvs_set_u16(h, "bport", port);
	nvs_commit(h);
	nvs_close(h);
}

void HomeWiFi::clear(){
	nvs_handle_t h;
	if(nvs_open(Ns, NVS_READWRITE, &h) != ESP_OK) return;
	nvs_erase_all(h);
	nvs_commit(h);
	nvs_close(h);
}

namespace {
	char lastProblemText[24] = "none";
}

void HomeWiFi::setLastProblem(const char* problem){
	strncpy(lastProblemText, problem && *problem ? problem : "none", sizeof(lastProblemText) - 1);
	lastProblemText[sizeof(lastProblemText) - 1] = 0;
}

const char* HomeWiFi::lastProblem(){
	return lastProblemText;
}

const char* HomeWiFi::resultName(Result r){
	switch(r){
		case Result::Ok: return "ok";
		case Result::NotConfigured: return "not_configured";
		case Result::NotFound: return "network_not_found";
		case Result::WrongPassword: return "wrong_password";
		case Result::Timeout: return "timeout";
		case Result::NoIp: return "no_ip_address";
		case Result::StartFailed: return "wifi_start_failed";
		default: return "failed";
	}
}

bool HomeWiFi::bringUp(){
	MemMark::mark("wifi: start");
	esp_netif_init();
	const esp_err_t loopErr = esp_event_loop_create_default();
	loopCreated = loopErr == ESP_OK; // INVALID_STATE: someone else's loop already exists - leave it alone
	events = xEventGroupCreate();
	lastReason = 0;
	associated = false;
	lastErr = 0;

	netif = esp_netif_create_default_wifi_sta();

	esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, [](void* arg, esp_event_base_t, int32_t, void* data){
		auto self = static_cast<HomeWiFi*>(arg);
		self->lastReason = static_cast<wifi_event_sta_disconnected_t*>(data)->reason;
		self->up = false;
		xEventGroupSetBits(self->events, Failed);
	}, this, &wifiHandler);
	esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, [](void* arg, esp_event_base_t, int32_t, void* data){
		auto self = static_cast<HomeWiFi*>(arg);
		self->ipAddr = static_cast<ip_event_got_ip_t*>(data)->ip_info.ip.addr;
		self->up = true;
		xEventGroupSetBits(self->events, GotIp);
	}, this, &ipHandler);
	esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_CONNECTED, [](void* arg, esp_event_base_t, int32_t, void*){
		static_cast<HomeWiFi*>(arg)->associated = true;
	}, this, &connHandler);

	wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
	lastErr = esp_wifi_init(&init);
	if(lastErr != ESP_OK){
		printf("[BBWIFI] event=init_failed err=%s ram=%u\n", esp_err_to_name(lastErr),
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
		return false;
	}
	started = true;
	MemMark::mark("wifi: init");
	return true;
}

bool HomeWiFi::scan(std::vector<Network>& out){
	out.clear();
	if(!bringUp()) return false;
	esp_wifi_set_mode(WIFI_MODE_STA);
	esp_wifi_set_ps(WIFI_PS_MIN_MODEM); // needed for Bluetooth coexistence
	if(esp_wifi_start() != ESP_OK) return false;
	if(esp_wifi_scan_start(nullptr, true) != ESP_OK) return false;

	uint16_t n = 24;
	std::vector<wifi_ap_record_t> recs(n);
	if(esp_wifi_scan_get_ap_records(&n, recs.data()) != ESP_OK) return false;
	for(uint16_t i = 0; i < n; ++i){
		const std::string name(reinterpret_cast<const char*>(recs[i].ssid));
		if(name.empty()) continue; // hidden network
		bool dup = false;
		for(auto& o : out){
			if(o.ssid == name){
				if(recs[i].rssi > o.rssi) o.rssi = recs[i].rssi;
				dup = true;
				break;
			}
		}
		if(!dup) out.push_back({ name, recs[i].rssi, recs[i].authmode == WIFI_AUTH_OPEN });
	}
	std::sort(out.begin(), out.end(), [](const Network& a, const Network& b){ return a.rssi > b.rssi; });
	return true;
}

/*
 * WiFi and Bluetooth share one radio, and while it waits for the controller the robot advertises every 20 ms. With
 * advertising on, WiFi packets are lost: uploads stall for seconds (24 KB: 27.9 s vs 2.6 s paused), and joining - the
 * same exchange of small packets with the router - can fail with reason 205 ("connection failed"; reported on a router
 * the robot could see fine). So for the whole WiFi session, join to stop(), WiFi gets priority and advertising pauses.
 * It's one pause on purpose: switching advertising back on after the join and off again for the upload raced (the
 * BLE stack starts and stops advertising asynchronously) and left it on during the upload. WiFi is only on for a few
 * seconds per AI question; a connected controller isn't affected, a waiting one connects right after.
 */
HomeWiFi::~HomeWiFi(){
	if(radioHeld || started || events) stop(); // a session that never started has nothing to undo
}

HomeWiFi::Result HomeWiFi::start(const Config& cfg, uint32_t timeoutMs){
	if(!radioHeld){
		radioHeld = true;
		esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
		ConMan.setQuiet(true);
	}
	return join(cfg, timeoutMs);
}

HomeWiFi::Result HomeWiFi::join(const Config& cfg, uint32_t timeoutMs){
	if(cfg.ssid.empty()) return Result::NotConfigured;

	if(!bringUp()) return Result::StartFailed;

	wifi_config_t wc = {};
	strncpy(reinterpret_cast<char*>(wc.sta.ssid), cfg.ssid.c_str(), sizeof(wc.sta.ssid));
	strncpy(reinterpret_cast<char*>(wc.sta.password), cfg.pass.c_str(), sizeof(wc.sta.password));
	wc.sta.threshold.authmode = cfg.pass.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA_PSK;
	wc.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
	wc.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
	wc.sta.pmf_cfg.capable = true;
	wc.sta.pmf_cfg.required = false;
	wc.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH; // WPA2/WPA3 mixed routers
	esp_wifi_set_mode(WIFI_MODE_STA);
	esp_wifi_set_config(WIFI_IF_STA, &wc);
	esp_wifi_set_ps(WIFI_PS_MIN_MODEM); // needed for Bluetooth coexistence
	lastErr = esp_wifi_start();
	if(lastErr == ESP_OK) lastErr = esp_wifi_connect();
	if(lastErr != ESP_OK){
		printf("[BBWIFI] event=start_failed err=%s ram=%u\n", esp_err_to_name(lastErr),
		       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
		return Result::StartFailed;
	}

	// Two tries: the first association sometimes times out or is refused on busy networks
	for(int attempt = 0; attempt < 2; ++attempt){
		const EventBits_t bits = xEventGroupWaitBits(events, GotIp | Failed, pdTRUE, pdFALSE, pdMS_TO_TICKS(timeoutMs));
		if(bits & GotIp){
			MemMark::mark("wifi: got ip");
			return Result::Ok;
		}
		if(!(bits & Failed)){
			// Joined but no address yet: the join may have used up most of the time, so give the router's DHCP its
			// own time instead of failing (retrying the association wouldn't help)
			if(associated) return waitForIp();
			// No answer at all yet: the driver is still scanning/joining (slow with Bluetooth sharing the radio),
			// so give it a second period rather than restarting the attempt
			if(attempt == 1) return Result::Timeout;
			continue;
		}
		if(lastReason == WIFI_REASON_NO_AP_FOUND) return Result::NotFound;
		if(lastReason == WIFI_REASON_AUTH_FAIL || lastReason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
		   lastReason == WIFI_REASON_HANDSHAKE_TIMEOUT || lastReason == WIFI_REASON_MIC_FAILURE){
			if(attempt == 1) return Result::WrongPassword;
		}
		associated = false;
		esp_wifi_connect();
	}
	return Result::Failed;
}

HomeWiFi::Result HomeWiFi::waitForIp(){
	// Joined the network but the router hasn't handed out an address. Routers sometimes drop the first request
	// (busy, or still clearing the robot's previous session from a few seconds ago), so wait a bit, then ask once more.
	for(int round = 0; round < 2; ++round){
		const EventBits_t bits = xEventGroupWaitBits(events, GotIp | Failed, pdTRUE, pdFALSE,
		                                             pdMS_TO_TICKS(round == 0 ? DhcpWaitMs : DhcpRetryWaitMs));
		if(bits & GotIp){
			if(round > 0) printf("[BBWIFI] event=dhcp_retry_ok\n");
			MemMark::mark("wifi: got ip");
			return Result::Ok;
		}
		if(bits & Failed) return Result::Failed; // dropped off the network while waiting
		if(round == 0 && netif){
			printf("[BBWIFI] event=dhcp_retry\n");
			esp_netif_dhcpc_stop(netif);
			esp_netif_dhcpc_start(netif);
		}
	}
	return Result::NoIp;
}

void HomeWiFi::stop(){
	if(radioHeld){
		radioHeld = false;
		ConMan.setQuiet(false);
		esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
	}
	up = false;
	if(started){
		esp_wifi_disconnect();
		esp_wifi_stop();
		esp_wifi_deinit();
		started = false;
	}
	if(wifiHandler){
		esp_event_handler_instance_unregister(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, wifiHandler);
		wifiHandler = nullptr;
	}
	if(connHandler){
		esp_event_handler_instance_unregister(WIFI_EVENT, WIFI_EVENT_STA_CONNECTED, connHandler);
		connHandler = nullptr;
	}
	if(ipHandler){
		esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, ipHandler);
		ipHandler = nullptr;
	}
	if(netif){
		esp_netif_destroy_default_wifi(netif);
		netif = nullptr;
	}
	if(loopCreated){
		esp_event_loop_delete_default();
		loopCreated = false;
	}
	if(events){
		vEventGroupDelete(events);
		events = nullptr;
	}
	MemMark::mark("wifi: stopped");
}

int HomeWiFi::rssi() const{
	wifi_ap_record_t ap;
	return up && esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
}
