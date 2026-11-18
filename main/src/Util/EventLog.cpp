#include "EventLog.h"
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <unistd.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>

/*
 * How lines get here: the firmware is linked with --wrap=printf and --wrap=puts (main/CMakeLists.txt), so every call
 * to the C library's printf / puts goes through __wrap_printf / __wrap_puts below first. They copy a "[BB...]" line
 * into the ring, then print it as before. ESP-IDF's own errors and warnings come through its log hook. Storing only
 * ever takes a spinlock for a memcpy: these run in any task, sometimes with other locks held or in a critical section
 * (an earlier version wrapped the stdout stream itself, took a mutex there and crashed at start-up:
 * "assert failed: spinlock_acquire ... (lock->count == 0)").
 */

namespace EventLog {

namespace {
	constexpr size_t MaxLines = 80;
	constexpr size_t MaxLine = 160;

	struct Entry {
		uint32_t upS;
		char text[MaxLine];
	};

	Entry* ring = nullptr; // PSRAM
	size_t next = 0, count = 0;
	portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
	vprintf_like_t prevLog = nullptr;

	// The kinds of lines worth keeping: what happened, not what's stored
	constexpr const char* KeepTags[] = { "[BBWIFI]", "[BBAI]", "[BBCONV]", "[BBMEM]", "[BBNVS]", "[BBCOM]", "[BBSR]",
	                                     "[BBDANCE]", "[BBVOICECMD]", "[BBROUT]" };

	bool keep(const char* s){
		if((s[0] == 'E' || s[0] == 'W') && s[1] == ' ' && s[2] == '(') return true; // ESP-IDF error / warning
		for(const char* tag : KeepTags){
			const size_t tl = strlen(tag);
			if(strncmp(s, tag, tl) != 0) continue;
			// Routines: only their runs (event=start/end), not the routine list (the owner's own words)
			if(strcmp(tag, "[BBROUT]") == 0) return strstr(s, "event=") != nullptr;
			// WiFi: the outcome and events, not the settings readout (network name) or a scan (nearby networks)
			if(strcmp(tag, "[BBWIFI]") == 0) return strstr(s, "ssid_hex=") == nullptr && strstr(s, "nets=") == nullptr;
			return true;
		}
		return false;
	}

	/** Blanks the values of address-like fields ("ip=1.2.3.4" -> "ip=-"). */
	void blank(char* s){
		for(const char* key : { "ip=", "brain=", "gw=", "mask=", "bssid=", "mac=" }){
			const size_t kl = strlen(key);
			for(char* p = s; (p = strstr(p, key)) != nullptr; p += kl){
				if(p != s && p[-1] != ' ') continue; // part of a longer key
				char* v = p + kl;
				char* end = v;
				while(*end && *end != ' ') ++end;
				if(end == v) continue;
				*v = '-';
				memmove(v + 1, end, strlen(end) + 1);
			}
		}
	}

	void record(const char* line){
		if(ring == nullptr || line == nullptr) return;
		// ESP-IDF colours its log lines: skip "\033[0;31m"
		if(line[0] == '\033'){
			const char* m = strchr(line, 'm');
			if(m == nullptr) return;
			line = m + 1;
		}
		if(!keep(line)) return;
		// Written straight into its slot (no copy on the caller's stack, which may be small); only string work in here
		portENTER_CRITICAL_SAFE(&mux);
		Entry& e = ring[next];
		e.upS = (uint32_t) (esp_timer_get_time() / 1000000);
		strncpy(e.text, line, MaxLine - 1);
		e.text[MaxLine - 1] = '\0';
		for(size_t n = strlen(e.text); n > 0 && (e.text[n - 1] == '\n' || e.text[n - 1] == '\r'); --n) e.text[n - 1] = '\0';
		for(char* p = e.text; (p = strstr(p, "\033[")) != nullptr;) *p = '\0'; // a trailing colour reset
		blank(e.text);
		if(e.text[0] != '\0'){
			next = (next + 1) % MaxLines;
			if(count < MaxLines) ++count;
		}
		portEXIT_CRITICAL_SAFE(&mux);
	}

	int logHook(const char* fmt, va_list ap){
		va_list copy;
		va_copy(copy, ap);
		char buf[MaxLine];
		vsnprintf(buf, sizeof(buf), fmt, copy);
		va_end(copy);
		record(buf);
		return prevLog != nullptr ? prevLog(fmt, ap) : vprintf(fmt, ap);
	}
}

void install(){
	if(ring != nullptr) return;
	ring = static_cast<Entry*>(heap_caps_calloc(MaxLines, sizeof(Entry), MALLOC_CAP_SPIRAM));
	prevLog = esp_log_set_vprintf(logHook);
}

std::string dump(){
	std::string out;
	if(ring == nullptr) return out;
	// Copy out under the lock (no allocating in a critical section), format after
	Entry* copy = static_cast<Entry*>(heap_caps_malloc(MaxLines * sizeof(Entry), MALLOC_CAP_SPIRAM));
	if(copy == nullptr) return out;
	portENTER_CRITICAL_SAFE(&mux);
	memcpy(copy, ring, MaxLines * sizeof(Entry));
	const size_t n = count, first = (next + MaxLines - count) % MaxLines;
	portEXIT_CRITICAL_SAFE(&mux);
	for(size_t i = 0; i < n; ++i){
		const Entry& e = copy[(first + i) % MaxLines];
		char head[16];
		snprintf(head, sizeof(head), "%lu ", (unsigned long) e.upS);
		out += head;
		out += e.text;
		out += '\n';
	}
	free(copy);
	return out;
}

void syncConsole(){
	fflush(stdout);
	fsync(fileno(stdout));
}

}

// --- the linker wraps (main/CMakeLists.txt) ---------------------------------------------------------------------------
extern "C" int __real_puts(const char* s);

extern "C" int __wrap_printf(const char* fmt, ...){
	va_list ap;
	va_start(ap, fmt);
	if(fmt != nullptr && fmt[0] == '[' && fmt[1] == 'B' && fmt[2] == 'B'){ // only the robot's own status lines
		va_list copy;
		va_copy(copy, ap);
		char buf[160];
		vsnprintf(buf, sizeof(buf), fmt, copy);
		va_end(copy);
		EventLog::record(buf);
	}
	const int r = vprintf(fmt, ap);
	va_end(ap);
	return r;
}

extern "C" int __wrap_puts(const char* s){
	if(s != nullptr && s[0] == '[' && s[1] == 'B' && s[2] == 'B') EventLog::record(s);
	return __real_puts(s);
}
