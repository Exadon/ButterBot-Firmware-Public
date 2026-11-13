#ifndef BUTTERBOT_FIRMWARE_BRAINLINK_H
#define BUTTERBOT_FIRMWARE_BRAINLINK_H

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include "Util/PSRAMAllocator.h"

/**
 * Fork: talking to the AI "brain" (Dimension Hopper's AI tab, on the owner's PC) over the home WiFi.
 *
 * Discovery: UDP broadcast "BBBRAIN?" to DiscoveryPort; the PC answers "BBBRAIN <tcp port>".
 * Conversation, one TCP connection per utterance:
 *   robot -> PC:  "BBTALK 1 rate=16000 bytes=<n> voice=<preset> fw=<version>\n" + n bytes of 16-bit mono PCM
 *   PC -> robot:  lines, then the connection closes
 *     "HEARD <what the PC transcribed>"   (for logs)
 *     "SAY <reply to speak>"
 *     "DO <activation phrase>"            (an existing voice command to run, e.g. "do a dance")
 *     "ERR <message>"
 */
namespace BrainLink {

inline constexpr uint16_t DiscoveryPort = 47800;

/** Broadcasts for the brain; on success fills ip (network order) and port. */
bool discover(uint32_t& ip, uint16_t& port, uint32_t timeoutMs);

/** Quick TCP check that a remembered brain address still answers. */
bool reachable(uint32_t ip, uint16_t port, uint32_t timeoutMs);

/** Diagnostic: uploads `bytes` of filler to a test receiver (BBSINK), returns milliseconds or -1. */
int64_t uploadTest(uint32_t ip, uint16_t port, uint32_t bytes, bool preferWifi);

struct Reply {
	bool ok = false;
	std::string heard;
	std::string say;
	std::string action;
	std::string error;
};

Reply converse(uint32_t ip, uint16_t port, const int16_t* pcm, size_t samples, const char* voice, uint32_t timeoutMs);

/**
 * Audio captured for one utterance. The AFE proc task appends to it; whoever started the recording waits for done.
 * Shared (shared_ptr) so a cancelled listen can't free it under the writer.
 */
struct Recording {
	static constexpr size_t MaxSamples = 16000 * 10; // 10 s
	PSRAMVector<int16_t> pcm;
	std::atomic<bool> done{ false };
	std::atomic<bool> heardSpeech{ false };
	uint32_t silentMs = 0;
	uint32_t totalMs = 0;
};

}

#endif //BUTTERBOT_FIRMWARE_BRAINLINK_H
