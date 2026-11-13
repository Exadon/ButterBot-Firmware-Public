#ifndef BUTTERBOT_FIRMWARE_PORTALROUTINE_H
#define BUTTERBOT_FIRMWARE_PORTALROUTINE_H

#include "Routine.h"

/**
 * "Open a portal": tells the controller to play its portal animation for PortalDurationMs and plays a
 * phaser sound effect, then holds for the same time so the next routine doesn't talk over it.
 */
class PortalRoutine : public Routine {
public:
	using Routine::Routine;

	TickingState tick(float deltaTime) override;

	static constexpr uint16_t PortalDurationMs = 2000;
};

#endif //BUTTERBOT_FIRMWARE_PORTALROUTINE_H
