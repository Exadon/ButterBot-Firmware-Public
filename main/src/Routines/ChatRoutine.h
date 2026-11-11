#ifndef BUTTERBOT_FIRMWARE_CHATROUTINE_H
#define BUTTERBOT_FIRMWARE_CHATROUTINE_H

#include <cstdint>
#include "Routine.h"

/**
 * Speaks chat line `index` (Phrase::Chat) and tells the controller which one, so it can show the reply.
 * Shared by every ChatRoutine<N>; returns Done.
 */
Routine::TickingState speakChatLine(uint8_t index);

/**
 * One routine type per chat line. Routines are created from argument-less factories (see
 * StateRoutineFactory.h), so the line index is baked in as a template parameter; the factories are
 * generated for 0..ChatLineCount-1 in ScenarioRoutineMappings.cpp.
 */
template<uint8_t N>
class ChatRoutine : public Routine {
public:
	using Routine::Routine;

	TickingState tick(float deltaTime) override{
		return speakChatLine(N);
	}
};

#endif //BUTTERBOT_FIRMWARE_CHATROUTINE_H
