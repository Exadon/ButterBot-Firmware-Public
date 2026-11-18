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
 * to the C library's printf / puts goes through __wrap_printf / __wrap_puts below first, and ESP-IDF's own log lines
 * through its log hook. Two hard rules, both learnt the hard way:
 *  - No locks but a spinlock, held only to reserve a slot: these run in any task, sometimes with other locks held
 *    (wrapping the stdout stream and taking a mutex there crashed at start-up: "assert failed: spinlock_acquire").
 *  - Nothing on the caller's stack: most of the robot's tasks have 4 KB stacks with little to spare, and a 160-byte
 *    buffer plus a second formatting pass made simple voice commands crash (1.1.1-exadon.10). So whether a line is
 *    kept is decided from the format text alone ("[BBWIFI] result=%s ..."), and a kept line is formatted straight
 *    into its slot in the ring (PSRAM). Lines that aren't kept cost one string compare.
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

	/** Past ESP-IDF's colour code ("\033[0;31m"), if any. */
	const char* skipColour(const char* s){
		if(s[0] != '\033') return s;
		const char* m = strchr(s, 'm');
		return m != nullptr ? m + 1 : s;
	}

	/** Decided from the format text (or the finished line - the same tests work on both). */
	bool keep(const char* s){
		s = skipColour(s);
		if((s[0] == 'E' || s[0] == 'W') && s[1] == ' ' && s[2] == '(') return true; // ESP-IDF error / warning
		if(s[0] != '[' || s[1] != 'B' || s[2] != 'B') return false;
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

	/** Takes the next slot (the oldest line gives way) and blanks it; nullptr before install(). */
	Entry* reserve(){
		if(ring == nullptr) return nullptr;
		portENTER_CRITICAL_SAFE(&mux);
		Entry* e = &ring[next];
		next = (next + 1) % MaxLines;
		if(count < MaxLines) ++count;
		e->text[0] = '\0';
		e->upS = (uint32_t) (esp_timer_get_time() / 1000000);
		portEXIT_CRITICAL_SAFE(&mux);
		return e;
	}

	/** Tidies a line written into its slot: colour codes and line ends off, addresses blanked. */
	void finish(Entry* e){
		char* t = e->text;
		const char* start = skipColour(t);
		if(start != t) memmove(t, start, strlen(start) + 1);
		for(char* p = t; (p = strstr(p, "\033[")) != nullptr;) *p = '\0';
		for(size_t n = strlen(t); n > 0 && (t[n - 1] == '\n' || t[n - 1] == '\r'); --n) t[n - 1] = '\0';
		blank(t);
	}

	void recordFormat(const char* fmt, va_list ap){
		if(fmt == nullptr || !keep(fmt)) return;
		Entry* e = reserve();
		if(e == nullptr) return;
		va_list copy;
		va_copy(copy, ap);
		vsnprintf(e->text, MaxLine, fmt, copy);
		va_end(copy);
		finish(e);
	}

	int logHook(const char* fmt, va_list ap){
		recordFormat(fmt, ap);
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
		if(e.text[0] == '\0') continue;
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
	EventLog::recordFormat(fmt, ap);
	const int r = vprintf(fmt, ap);
	va_end(ap);
	return r;
}

extern "C" int __wrap_puts(const char* s){
	if(s != nullptr && EventLog::keep(s)){
		if(EventLog::Entry* e = EventLog::reserve()){
			strncpy(e->text, s, EventLog::MaxLine - 1);
			e->text[EventLog::MaxLine - 1] = '\0';
			EventLog::finish(e);
		}
	}
	return __real_puts(s);
}
