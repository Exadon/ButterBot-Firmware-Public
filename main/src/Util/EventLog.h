#ifndef BUTTERBOT_FIRMWARE_EVENTLOG_H
#define BUTTERBOT_FIRMWARE_EVENTLOG_H

#include <string>

/**
 * Fork: the robot's recent events, for Dimension Hopper's problem report.
 *
 * Most problems happen with nobody watching the USB console (AI questions are asked unplugged), so the robot keeps
 * its last status lines in PSRAM and hands them over when it's plugged in ("events" on the console). The firmware is
 * linked with --wrap=printf/puts: the robot's own "[BB...]" lines are copied as they're printed (see EventLog.cpp),
 * and ESP-IDF's errors and warnings come through its log hook. Kept: [BBWIFI], [BBAI], [BBCONV], [BBMEM], [BBNVS],
 * routine runs and the like, with the uptime; never the lines that answer settings queries (names, the owner's
 * lines, WiFi network, nearby networks), and addresses are blanked before a line is stored.
 * Kept in RAM only: lost when the robot switches off.
 */
namespace EventLog {

/** Sets up the ring and the log hook. Call early in Application::begin(); lines printed before that aren't kept. */
void install();

/** The kept lines, oldest first, one per line: "<uptime s> <line>". */
std::string dump();

/** fflush + fsync on the console: waits until the UART has sent a long line. */
void syncConsole();

}

#endif //BUTTERBOT_FIRMWARE_EVENTLOG_H
