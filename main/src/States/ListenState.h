#ifndef BUTTERBOT_FIRMWARE_LISTENSTATE_H
#define BUTTERBOT_FIRMWARE_LISTENSTATE_H

#include "State.h"
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

private:
    Com* com = nullptr; // Was uninitialized; the ctor can bail out before setting it and aborted() null-checks it

    bool timedOut = false;

    void onRC();
    void onPhrase(bool recognized, int index, const std::string& transcript, float confidence);
    void onButtonEvent(int button, ISRButtonInput::Action action);
    void onCommand(Ctrl::Command cmd);

    void aborted();

	static std::vector<AudioFrontend::Phrase> phrases;
};

#endif //BUTTERBOT_FIRMWARE_LISTENSTATE_H
