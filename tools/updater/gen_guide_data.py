"""
Builds guide_data.json for Dimension Hopper's Guide tab from the firmware's own sources, so the voice-command
list always matches what the robot actually understands:
  - components/ButterBot-Common/src/Scenarios.h  (every voice trigger + which action it runs)
  - components/ButterBot-Common/src/Phrases.cpp  (the replies of the chat lines)
Run automatically by build_exe.ps1; also runnable by hand:  python gen_guide_data.py
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
COMMON = HERE.parent.parent / "components" / "ButterBot-Common" / "src"

# scenario -> (category, label). Data-dependent ones (dice, movement, faces, activity) are refined below.
SCENARIOS = {
    "Fact": ("Fun", "Tell a fact"),
    "Joke": ("Fun", "Tell a joke"),
    "PassTheButter": ("Fun", "Pass the butter"),
    "YouPassButter": ("Fun", "You pass butter"),
    "Profanity": ("Fun", "Insults (snarky comeback)"),
    "EightBall": ("Fun", "Magic eight ball"),
    "Dance": ("Fun", "Dance"),
    "OpenPortal": ("Fun", "Open a portal (animation on the controller)"),
    "WhatsThis": ("Fun", "What is this? (camera object recognition)"),
    "CurrentTime": ("Fun", "Current time"),
    "DiceRoll": ("Dice", "Roll dice"),
    "VoiceControl": ("Movement", "Move"),
    "FaceDetect": ("Faces", "Faces"),
    "FaceDetectForget": ("Faces", "Forget the owner's face"),
    "IR_train": ("IR Remote", "Learn an IR command"),
    "IR_list": ("IR Remote", "List IR commands"),
    "IR_forget": ("IR Remote", "Forget an IR command"),
    "IR_forgetAll": ("IR Remote", "Forget all IR commands"),
    "IR_action": ("IR Remote", "Run a learned IR command"),
    "LEDTurnOn": ("LED Module", "LED on"),
    "LEDTurnOff": ("LED Module", "LED off"),
    "LEDStrobe": ("LED Module", "LED strobe"),
    "LEDBreathe": ("LED Module", "LED breathe"),
    "LEDFaster": ("LED Module", "LED faster"),
    "LEDSlower": ("LED Module", "LED slower"),
    "TempHumModule": ("Climate Module", "Temperature & humidity"),
    "TempHumScaleCelsius": ("Climate Module", "Use Celsius"),
    "TempHumScaleFahrenheit": ("Climate Module", "Use Fahrenheit"),
    "TempHumScaleKelvin": ("Climate Module", "Use Kelvin"),
    "GasModule": ("Sensors", "Air quality (gas module)"),
    "IntruderDetectionOn": ("Sensors", "Intruder detection on"),
    "IntruderDetectionOff": ("Sensors", "Intruder detection off"),
    "PhoneListNotifs": ("Phone", "Read notifications"),
    "PhoneWhatsPlaying": ("Phone", "What's playing?"),
    "PhoneNextSong": ("Phone", "Next song"),
    "PhonePrevSong": ("Phone", "Previous song"),
    "PhonePlayMusic": ("Phone", "Play music"),
    "PhoneStopMusic": ("Phone", "Stop music"),
    "SettingsActivity": ("Robot", "Activity level"),
    "Shutdown": ("Robot", "Turn off"),
    "Chat": ("Chat", "Chat"),
}
CATEGORY_ORDER = ["Fun", "Chat", "Dice", "Movement", "Faces", "Phone", "IR Remote", "LED Module",
                  "Climate Module", "Sensors", "Robot", "Other"]


def refine(scenario: str, data: str, label: str) -> str:
    """Turn the ScenarioData expression into a nicer label for multi-variant scenarios."""
    if scenario == "DiceRoll":
        m = re.search(r"DiceType::(\w+)", data)
        return "Roll dice (asks which)" if not m or m.group(1) == "None" else f"Roll a {m.group(1)}"
    if scenario == "VoiceControl":
        m = re.search(r"Direction::(\w+)", data)
        return {"Forward": "Go forward", "Backward": "Go backward", "Left": "Turn left", "Right": "Turn right",
                "Rotate": "Turn around"}.get(m.group(1) if m else "", label)
    if scenario == "FaceDetect":
        m = re.search(r"Phase::(\w+)", data)
        return {"Detect": "Who is this?", "Remember": "Remember my face"}.get(m.group(1) if m else "", label)
    if scenario == "SettingsActivity":
        m = re.search(r"Level::(\w+)", data)
        lvl = m.group(1) if m else "None"
        return "Activity level (asks which)" if lvl == "None" else f"Activity: {lvl.lower()}"
    return label


def main():
    scen = (COMMON / "Scenarios.h").read_text(encoding="utf-8")
    phrases = (COMMON / "Phrases.cpp").read_text(encoding="utf-8")

    chat_block = re.search(r"ChatPhrases = std::to_array<Phrases::PhraseOutput>\(\{(.*?)\n\t\}\);", phrases, re.S)
    replies = re.findall(r'\{ "((?:[^"\\]|\\.)*)", 0\.0f', chat_block.group(1)) if chat_block else []

    # some entries carry a /*RoutineName*/ comment after the scenario data
    acts = re.findall(r'\{ BB::Action::Scenario::(\w+), (\w*\{[^}]*\})\s*(?:/\*.*?\*/)?\s*, "([^"]+)"', scen)
    groups = {}  # (category, label) -> {"say": [...], "reply": str}
    order = []
    for scenario, data, trigger in acts:
        cat, label = SCENARIOS.get(scenario, ("Other", scenario))
        reply = ""
        if scenario == "Chat":
            m = re.search(r"\{\s*(\d+)\s*\}", data)
            idx = int(m.group(1)) if m else -1
            reply = replies[idx] if 0 <= idx < len(replies) else ""
            label = trigger
        else:
            label = refine(scenario, data, label)
        key = (cat, label)
        if key not in groups:
            groups[key] = {"category": cat, "action": label, "say": [], "reply": reply}
            order.append(key)
        if trigger not in groups[key]["say"]:
            groups[key]["say"].append(trigger)

    entries = [groups[k] for k in order]
    entries.sort(key=lambda e: CATEGORY_ORDER.index(e["category"]) if e["category"] in CATEGORY_ORDER else 99)
    out = {"triggerCount": len(acts), "entries": entries}
    (HERE / "guide_data.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"guide_data.json: {len(acts)} triggers -> {len(entries)} actions, {len(replies)} chat replies")


if __name__ == "__main__":
    main()
