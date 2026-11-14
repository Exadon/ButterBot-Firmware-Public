#ifndef BUTTERBOT_FIRMWARE_LISTENSTATE_H
#define BUTTERBOT_FIRMWARE_LISTENSTATE_H

#include <string>
#include "State.h"
#include <memory>
#include "CtrlData.h"
#include "Audio/AudioFrontend.h"
#include "Services/Com.h"
#include "Services/ISRButtonInput.h"

class ListenState : public State {
public:
	const char* name() const override { return "listen"; }
    explicit ListenState(BBStateMachine* sm);
    virtual ~ListenState() override;

    static void preallocate();

    // Fork: end the pause on AI after a failed attempt (Dimension Hopper's Start sends "ai on" once the brain runs)
    static void clearAiPause();

    /** Fork: the last AI conversation (what was heard, what the robot answered), for the controller's status page. */
    static void lastConversation(std::string& heard, std::string& said);

    void tick(float deltaTime) override;
    int64_t getDynamicTickInterval() const override;

    struct AiSession; // fork: AI brain conversation (ListenState.cpp)

private:
    Com* com = nullptr; // Was uninitialized; the ctor can bail out before setting it and aborted() null-checks it

    bool timedOut = false;

    void onRC();
    void onPhrase(bool recognized, int index, const std::string& transcript, float confidence);
    void onButtonEvent(int button, ISRButtonInput::Action action);
    void onCommand(Ctrl::Command cmd);

    void aborted();

    // Fork: the usual offline command list (also the fallback when the AI brain can't be reached)
    void startOffline(const class Application* app);
    void fallBackToOffline(const class Application* app, const char* why);

	static std::vector<AudioFrontend::Phrase> phrases;

    std::shared_ptr<AiSession> ai;
};

#endif //BUTTERBOT_FIRMWARE_LISTENSTATE_H
