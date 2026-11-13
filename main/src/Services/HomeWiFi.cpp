#include "HomeWiFi.h"
#include <algorithm>
#include <cstring>
#include <esp_event.h>
#include <esp_wifi.h>
#include <nvs.h>
#include "Util/MemMark.h"
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

const char* HomeWiFi::resultName(Result r){
	switch(r){
		case Result::Ok: return "ok";
		case Result::NotConfigured: return "not_configured";
		case Result::NotFound: return "network_not_found";
		case Result::WrongPassword: return "wrong_password";
		case Result::Timeout: return "timeout";
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

	wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
	if(esp_wifi_init(&init) != ESP_OK){
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

HomeWiFi::Result HomeWiFi::start(const Config& cfg, uint32_t timeoutMs){
	if(cfg.ssid.empty()) return Result::NotConfigured;

	if(!bringUp()) return Result::Failed;

	wifi_config_t wc = {};
	strncpy(reinterpret_cast<char*>(wc.sta.ssid), cfg.ssid.c_str(), sizeof(wc.sta.ssid));
	strncpy(reinterpret_cast<char*>(wc.sta.password), cfg.pass.c_str(), sizeof(wc.sta.password));
	wc.sta.threshold.authmode = cfg.pass.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA_PSK;
	wc.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
	wc.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
	esp_wifi_set_mode(WIFI_MODE_STA);
	esp_wifi_set_config(WIFI_IF_STA, &wc);
	esp_wifi_set_ps(WIFI_PS_MIN_MODEM); // needed for Bluetooth coexistence
	esp_wifi_start();
	esp_wifi_connect();

	// A failed attempt may be retried once (the first association sometimes times out on busy networks)
	for(int attempt = 0; attempt < 2; ++attempt){
		const EventBits_t bits = xEventGroupWaitBits(events, GotIp | Failed, pdTRUE, pdFALSE, pdMS_TO_TICKS(timeoutMs));
		if(bits & GotIp){
			MemMark::mark("wifi: got ip");
			return Result::Ok;
		}
		if(!(bits & Failed)) return Result::Timeout;
		if(lastReason == WIFI_REASON_NO_AP_FOUND) return Result::NotFound;
		if(lastReason == WIFI_REASON_AUTH_FAIL || lastReason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
		   lastReason == WIFI_REASON_HANDSHAKE_TIMEOUT || lastReason == WIFI_REASON_MIC_FAILURE){
			if(attempt == 1) return Result::WrongPassword;
		}
		esp_wifi_connect();
	}
	return Result::Failed;
}

void HomeWiFi::stop(){
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
