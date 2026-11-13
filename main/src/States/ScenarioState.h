#ifndef BUTTERBOT_FIRMWARE_SCENARIOSTATE_H
#define BUTTERBOT_FIRMWARE_SCENARIOSTATE_H

#include "State.h"
#include "Routines/Routine.h"

class ScenarioState : public State {
public:
	const char* name() const override { return "scenario"; }
    explicit ScenarioState(BBStateMachine* sm);
    ~ScenarioState() override;

    virtual void tick(float deltaTime) override;
    virtual int64_t getDynamicTickInterval() const override;

private:
    Routine::TickingState lastTickingState = Routine::TickingState::None;

private:
    void onRC();
};

#endif //BUTTERBOT_FIRMWARE_SCENARIOSTATE_H