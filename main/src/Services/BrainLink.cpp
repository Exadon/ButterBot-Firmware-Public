#include "BrainLink.h"
#include "Conversation.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <fcntl.h>
#include <lwip/sockets.h>
#include <lwip/inet.h>
#include <esp_app_desc.h>
#include <esp_coexist.h>
#include <esp_timer.h>
#include <vector>
#include "Util/PSRAMAllocator.h"
#include "BLE/ConMan.h"
#include "Log/Log.h"
#include "Util/Snapshot.h"

DEFINE_LOG(BrainLink)

namespace BrainLink {

namespace {
	void setTimeouts(int s, uint32_t ms){
		timeval tv{ .tv_sec = (time_t) (ms / 1000), .tv_usec = (suseconds_t) ((ms % 1000) * 1000) };
		setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
		setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	}

	/**
	 * TCP connect that really gives up after timeoutMs. lwIP's blocking connect() ignores SO_SNDTIMEO and waits for
	 * the SYN retries to run out (~20 s) when nothing answers - e.g. the brain isn't running and the PC's firewall
	 * silently drops the attempt. That made "wifi test" report too late for Dimension Hopper, and stalled
	 * conversations whenever the brain was off. So: non-blocking connect + select().
	 */
	int connectTo(uint32_t ip, uint16_t port, uint32_t timeoutMs){
		const int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
		if(s < 0) return -1;
		setTimeouts(s, timeoutMs);
		sockaddr_in addr{};
		addr.sin_family = AF_INET;
		addr.sin_port = htons(port);
		addr.sin_addr.s_addr = ip;
		const int flags = fcntl(s, F_GETFL, 0);
		fcntl(s, F_SETFL, flags | O_NONBLOCK);
		if(connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0){
			if(errno != EINPROGRESS){
				close(s);
				return -1;
			}
			fd_set w;
			FD_ZERO(&w);
			FD_SET(s, &w);
			timeval tv{ .tv_sec = (time_t) (timeoutMs / 1000), .tv_usec = (suseconds_t) ((timeoutMs % 1000) * 1000) };
			int err = 0;
			socklen_t len = sizeof(err);
			if(select(s + 1, nullptr, &w, nullptr, &tv) <= 0 || getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len) != 0 || err != 0){
				close(s);
				return -1;
			}
		}
		fcntl(s, F_SETFL, flags); // back to blocking for the send/recv that follow (they honour the timeouts above)
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

	// Radio modes while WiFi uploads: 0 balance, 1 prefer WiFi, 2 prefer WiFi + pause BLE advertising
	void radioFor(int mode, bool on){
		if(mode >= 1) esp_coex_preference_set(on ? ESP_COEX_PREFER_WIFI : ESP_COEX_PREFER_BALANCE);
		if(mode >= 2) ConMan.setQuiet(on);
	}

	/*
	 * Shrinking the upload (measured on the PC with Whisper first: same words, a quarter of the bytes):
	 *  - trim: keep only the speech plus 200 ms either side (the recording ends with ~0.7 s of silence by design)
	 *  - 16 -> 8 kHz: a small half-band low-pass, then every other sample. The PC interpolates back to 16 kHz.
	 * Set SendRate to 16000 to send full-rate audio again.
	 */
	constexpr uint32_t SendRate = 8000;
	constexpr size_t TrimFrame = 320;   // 20 ms at 16 kHz
	constexpr size_t TrimMargin = 10;   // frames kept around the speech

	void trimSilence(const int16_t* pcm, size_t samples, size_t& from, size_t& to){
		from = 0;
		to = samples;
		const size_t n = samples / TrimFrame;
		if(n == 0) return;
		std::vector<uint32_t, PSRAMAllocator<uint32_t>> energy(n);
		for(size_t f = 0; f < n; ++f){
			uint32_t sum = 0;
			for(size_t i = 0; i < TrimFrame; ++i) sum += (uint32_t) std::abs((int32_t) pcm[f * TrimFrame + i]);
			energy[f] = sum / TrimFrame;
		}
		std::vector<uint32_t, PSRAMAllocator<uint32_t>> sorted(energy);
		std::nth_element(sorted.begin(), sorted.begin() + n / 10, sorted.end());
		const uint32_t threshold = std::max<uint32_t>(sorted[n / 10] * 4, 200); // 4x the quietest tenth = speech
		size_t first = n, last = 0;
		for(size_t f = 0; f < n; ++f){
			if(energy[f] > threshold){
				if(first == n) first = f;
				last = f;
			}
		}
		if(first == n) return; // nothing clearly louder than the background: send it all
		from = (first > TrimMargin ? first - TrimMargin : 0) * TrimFrame;
		to = std::min(n, last + 1 + TrimMargin) * TrimFrame;
	}

	int16_t halfbandAt(const int16_t* pcm, size_t samples, size_t i){
		static constexpr int Taps[] = { -1, 0, 9, 16, 9, 0, -1 }; // /32
		int32_t acc = 0;
		for(int k = 0; k < 7; ++k){
			const int64_t idx = (int64_t) i + k - 3;
			if(idx >= 0 && idx < (int64_t) samples) acc += Taps[k] * (int32_t) pcm[idx];
		}
		acc /= 32;
		return (int16_t) std::clamp<int32_t>(acc, -32768, 32767);
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

int64_t uploadTest(uint32_t ip, uint16_t port, uint32_t bytes, int radioMode){
	const int s = connectTo(ip, port, 30000);
	if(s < 0) return -1;
	std::vector<uint8_t, PSRAMAllocator<uint8_t>> filler(bytes, 0x55);
	char head[64];
	const int hn = snprintf(head, sizeof(head), "BBSINK bytes=%u\n", (unsigned) bytes);
	radioFor(radioMode, true);
	const int64_t t0 = esp_timer_get_time();
	const bool ok = sendAll(s, head, (size_t) hn) && sendAll(s, filler.data(), filler.size());
	char ack[8];
	const bool acked = ok && recv(s, ack, sizeof(ack), 0) > 0; // the receiver answers once it has everything
	const int64_t ms = (esp_timer_get_time() - t0) / 1000;
	radioFor(radioMode, false);
	close(s);
	return acked ? ms : -1;
}

Reply converse(uint32_t ip, uint16_t port, const int16_t* pcm, size_t samples, const char* voice, uint32_t timeoutMs,
               bool canLook){
	Reply r;
	const int s = connectTo(ip, port, timeoutMs);
	if(s < 0){
		r.error = "brain_unreachable";
		return r;
	}

	size_t from = 0, to = samples;
	trimSilence(pcm, samples, from, to);
	const int16_t* speech = pcm + from;
	const size_t speechSamples = to - from;
	const size_t step = SendRate == 8000 ? 2 : 1;
	std::vector<uint8_t, PSRAMAllocator<uint8_t>> ulaw(speechSamples / step);
	for(size_t j = 0; j < ulaw.size(); ++j){
		ulaw[j] = toUlaw(step == 2 ? halfbandAt(speech, speechSamples, j * 2) : speech[j]);
	}
	printf("[BBAI] event=shrunk ms=%u kept_ms=%u bytes=%u\n", (unsigned) (samples / 16), (unsigned) (speechSamples / 16),
	       (unsigned) ulaw.size());

	char head[176];
	// conv=1: conversation mode, so the brain keeps the whole session as context (older brains ignore the field)
	// look=1: the brain may ask for a photo (LOOK) before it answers
	const int hn = snprintf(head, sizeof(head), "BBTALK 1 rate=%u fmt=ulaw bytes=%u voice=%s fw=%s conv=%d look=%d\n",
	                        (unsigned) SendRate, (unsigned) ulaw.size(), voice, esp_app_get_description()->version,
	                        Conversation::active() ? 1 : 0, canLook ? 1 : 0);
	// WiFi and Bluetooth share one radio: for the whole WiFi session (HomeWiFi start to stop) WiFi has priority and
	// Bluetooth advertising is paused. Measured 2026-11-16 with "wifi bench", 24 KB, signal -42 dBm: advertising on
	// 27.9 s with stalls up to 8.7 s (packets lost while it advertises every 20 ms, then TCP waits out its retry timer);
	// paused 2.6 s. Not switched here per upload: resuming and pausing it again a moment later raced and left it on.
	const bool sent = sendAll(s, head, (size_t) hn) && sendAll(s, ulaw.data(), ulaw.size(), true);
	if(!sent){
		close(s);
		r.error = "send_failed";
		return r;
	}
	// The brain reads exactly bytes=<n>, so the upload needs no end-of-stream; without look the write side closes as before
	if(!canLook) shutdown(s, SHUT_WR);

	// Read the reply lines until the PC closes the connection. A "LOOK" line is answered right away with a photo.
	std::string in;
	char buf[256];
	size_t scanned = 0;
	for(;;){
		const int n = recv(s, buf, sizeof(buf), 0);
		if(n <= 0) break;
		in.append(buf, (size_t) n);
		if(in.size() > 4096) break;
		while(canLook){
			const size_t nl = in.find('\n', scanned);
			if(nl == std::string::npos) break;
			if(r.looked || in.compare(scanned, nl - scanned, "LOOK") != 0){
				scanned = nl + 1;
				continue;
			}
			r.looked = true;
			in.erase(scanned, nl + 1 - scanned);
			Snapshot::Photo photo = Snapshot::take();
			char ih[64];
			const int ihn = photo.jpg != nullptr
			                ? snprintf(ih, sizeof(ih), "BBIMG bytes=%u w=%u h=%u\n", (unsigned) photo.len, (unsigned) photo.w, (unsigned) photo.h)
			                : snprintf(ih, sizeof(ih), "BBIMG bytes=0 why=%s\n", photo.why);
			const bool ok = sendAll(s, ih, (size_t) ihn) && (photo.jpg == nullptr || sendAll(s, photo.jpg, photo.len));
			free(photo.jpg);
			shutdown(s, SHUT_WR);
			if(!ok) r.error = "photo_send_failed";
		}
		if(!r.error.empty()) break;
	}
	close(s);
	if(!r.error.empty()) return r;

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
