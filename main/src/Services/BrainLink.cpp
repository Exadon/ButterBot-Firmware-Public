#include "BrainLink.h"
#include <cstdio>
#include <cstring>
#include <lwip/sockets.h>
#include <lwip/inet.h>
#include <esp_app_desc.h>
#include <esp_coexist.h>
#include <esp_timer.h>
#include <vector>
#include "Util/PSRAMAllocator.h"
#include "Log/Log.h"

DEFINE_LOG(BrainLink)

namespace BrainLink {

namespace {
	void setTimeouts(int s, uint32_t ms){
		timeval tv{ .tv_sec = (time_t) (ms / 1000), .tv_usec = (suseconds_t) ((ms % 1000) * 1000) };
		setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
		setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	}

	int connectTo(uint32_t ip, uint16_t port, uint32_t timeoutMs){
		const int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
		if(s < 0) return -1;
		setTimeouts(s, timeoutMs);
		sockaddr_in addr{};
		addr.sin_family = AF_INET;
		addr.sin_port = htons(port);
		addr.sin_addr.s_addr = ip;
		if(connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0){
			close(s);
			return -1;
		}
		return s;
	}

	bool sendAll(int s, const void* data, size_t len, bool progress = false){
		auto p = static_cast<const uint8_t*>(data);
		const size_t total = len;
		const int64_t t0 = esp_timer_get_time();
		size_t nextReport = 16384;
		while(len > 0){
			const int n = send(s, p, len > 4096 ? 4096 : len, 0);
			if(n <= 0){
				if(progress) printf("[BBAI] event=upload_stalled sent=%u of=%u ms=%lld\n", (unsigned) (total - len), (unsigned) total,
				                    (esp_timer_get_time() - t0) / 1000);
				return false;
			}
			p += n;
			len -= (size_t) n;
			if(progress && total - len >= nextReport){
				printf("[BBAI] event=upload sent=%u of=%u ms=%lld\n", (unsigned) (total - len), (unsigned) total,
				       (esp_timer_get_time() - t0) / 1000);
				nextReport += 16384;
			}
		}
		if(progress) printf("[BBAI] event=uploaded bytes=%u ms=%lld\n", (unsigned) total, (esp_timer_get_time() - t0) / 1000);
		return true;
	}

	// G.711 mu-law: 8 bits per sample, half the bytes of 16-bit PCM, plenty for speech recognition
	uint8_t toUlaw(int16_t pcm){
		constexpr int Bias = 0x84, Clip = 32635;
		int sample = pcm;
		const int sign = (sample >> 8) & 0x80;
		if(sign) sample = -sample;
		if(sample > Clip) sample = Clip;
		sample += Bias;
		int exponent = 7;
		for(int mask = 0x4000; (sample & mask) == 0 && exponent > 0; mask >>= 1) exponent--;
		const int mantissa = (sample >> (exponent + 3)) & 0x0F;
		return static_cast<uint8_t>(~(sign | (exponent << 4) | mantissa));
	}
}

bool discover(uint32_t& ip, uint16_t& port, uint32_t timeoutMs){
	const int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if(s < 0) return false;
	int yes = 1;
	setsockopt(s, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes));
	setTimeouts(s, 400);

	sockaddr_in to{};
	to.sin_family = AF_INET;
	to.sin_port = htons(DiscoveryPort);
	to.sin_addr.s_addr = htonl(INADDR_BROADCAST);

	const uint32_t tries = timeoutMs / 400 > 0 ? timeoutMs / 400 : 1;
	bool found = false;
	for(uint32_t i = 0; i < tries && !found; ++i){
		static constexpr char Ask[] = "BBBRAIN?";
		sendto(s, Ask, sizeof(Ask) - 1, 0, reinterpret_cast<sockaddr*>(&to), sizeof(to));
		char buf[64];
		sockaddr_in from{};
		socklen_t fromLen = sizeof(from);
		const int n = recvfrom(s, buf, sizeof(buf) - 1, 0, reinterpret_cast<sockaddr*>(&from), &fromLen);
		if(n <= 0) continue;
		buf[n] = '\0';
		unsigned p = 0;
		if(sscanf(buf, "BBBRAIN %u", &p) == 1 && p > 0 && p < 65536){
			ip = from.sin_addr.s_addr;
			port = (uint16_t) p;
			found = true;
		}
	}
	close(s);
	return found;
}

bool reachable(uint32_t ip, uint16_t port, uint32_t timeoutMs){
	if(ip == 0 || port == 0) return false;
	const int s = connectTo(ip, port, timeoutMs);
	if(s < 0) return false;
	static constexpr char Ping[] = "BBPING\n";
	send(s, Ping, sizeof(Ping) - 1, 0);
	close(s);
	return true;
}

int64_t uploadTest(uint32_t ip, uint16_t port, uint32_t bytes, bool preferWifi){
	const int s = connectTo(ip, port, 30000);
	if(s < 0) return -1;
	std::vector<uint8_t, PSRAMAllocator<uint8_t>> filler(bytes, 0x55);
	char head[64];
	const int hn = snprintf(head, sizeof(head), "BBSINK bytes=%u\n", (unsigned) bytes);
	if(preferWifi) esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
	const int64_t t0 = esp_timer_get_time();
	const bool ok = sendAll(s, head, (size_t) hn) && sendAll(s, filler.data(), filler.size());
	char ack[8];
	const bool acked = ok && recv(s, ack, sizeof(ack), 0) > 0; // the receiver answers once it has everything
	const int64_t ms = (esp_timer_get_time() - t0) / 1000;
	if(preferWifi) esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
	close(s);
	return acked ? ms : -1;
}

Reply converse(uint32_t ip, uint16_t port, const int16_t* pcm, size_t samples, const char* voice, uint32_t timeoutMs){
	Reply r;
	const int s = connectTo(ip, port, timeoutMs);
	if(s < 0){
		r.error = "brain_unreachable";
		return r;
	}

	std::vector<uint8_t, PSRAMAllocator<uint8_t>> ulaw(samples);
	for(size_t i = 0; i < samples; ++i) ulaw[i] = toUlaw(pcm[i]);

	char head[160];
	const int hn = snprintf(head, sizeof(head), "BBTALK 1 rate=16000 fmt=ulaw bytes=%u voice=%s fw=%s\n",
	                        (unsigned) ulaw.size(), voice, esp_app_get_description()->version);
	// WiFi and Bluetooth share one radio; for the upload WiFi gets priority (the controller link just slows briefly)
	esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
	const bool sent = sendAll(s, head, (size_t) hn) && sendAll(s, ulaw.data(), ulaw.size(), true);
	esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
	if(!sent){
		close(s);
		r.error = "send_failed";
		return r;
	}
	shutdown(s, SHUT_WR);

	// Read the reply lines until the PC closes the connection
	std::string in;
	char buf[256];
	for(;;){
		const int n = recv(s, buf, sizeof(buf), 0);
		if(n <= 0) break;
		in.append(buf, (size_t) n);
		if(in.size() > 4096) break;
	}
	close(s);

	size_t pos = 0;
	while(pos < in.size()){
		size_t end = in.find('\n', pos);
		if(end == std::string::npos) end = in.size();
		const std::string line = in.substr(pos, end - pos);
		pos = end + 1;
		if(line.rfind("HEARD ", 0) == 0) r.heard = line.substr(6);
		else if(line.rfind("SAY ", 0) == 0) r.say = line.substr(4);
		else if(line.rfind("DO ", 0) == 0) r.action = line.substr(3);
		else if(line.rfind("ERR ", 0) == 0) r.error = line.substr(4);
	}
	r.ok = r.error.empty() && (!r.say.empty() || !r.action.empty());
	if(!r.ok && r.error.empty()) r.error = in.empty() ? "no_reply" : "empty_reply";
	return r;
}

}
