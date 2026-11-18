#ifndef BUTTERBOT_FIRMWARE_BEEPS_H
#define BUTTERBOT_FIRMWARE_BEEPS_H

class Audio;

/**
 * Fork: short beeps made on the fly (16 kHz mono PCM in PSRAM, no files, no internal RAM), for the AI listen:
 *   heard()    - a quick rising two-note chirp: "got it, working on it"
 *   thinking() - one soft swelling tone, repeated by the caller about every 1.2 s while it connects and thinks
 * Each plays only if nothing else is playing. Personal::cueOn turns them off (Personalize tab).
 */
namespace Beeps {

void heard(Audio* audio);
void thinking(Audio* audio);

}

#endif //BUTTERBOT_FIRMWARE_BEEPS_H
