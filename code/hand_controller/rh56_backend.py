"""RH56F2 hand backend used by the single repository arm service.

This module deliberately covers hand-only actions.  Panthera movement is a
separate backend because the hardware handoff says its Linux SDK and transport
are not available yet.  Actions that require the arm therefore fail clearly
instead of claiming that a physical rescue action completed.
"""
from __future__ import annotations

import time

from hand_controller.rh56f2_driver import RH56F2

TH, IX, MI, RG, PK, TR = "thumb", "index", "middle", "ring", "pinky", "thumb_rot"

POSE_VOCAB = {
    "rock": {TH: 10, IX: 10, MI: 10, RG: 10, PK: 10, TR: 50},
    "paper": {TH: 90, IX: 95, MI: 95, RG: 95, PK: 95, TR: 50},
    "scissors": {TH: 15, IX: 90, MI: 90, RG: 10, PK: 10, TR: 50},
    "thumbs_up": {TH: 95, IX: 10, MI: 10, RG: 10, PK: 10, TR: 60},
    "hold_card": {TH: 40, IX: 40, MI: 10, RG: 10, PK: 10, TR: 70},
    "release": {TH: 90, IX: 95, MI: 95, RG: 95, PK: 95, TR: 50},
    "fingers_1": {TH: 10, IX: 90, MI: 10, RG: 10, PK: 10, TR: 50},
    "fingers_2": {TH: 10, IX: 90, MI: 90, RG: 10, PK: 10, TR: 50},
    "fingers_3": {TH: 10, IX: 90, MI: 90, RG: 90, PK: 10, TR: 50},
    "fingers_4": {TH: 10, IX: 90, MI: 90, RG: 90, PK: 90, TR: 50},
    "fingers_5": {TH: 90, IX: 95, MI: 95, RG: 95, PK: 95, TR: 50},
}

SEQUENCES = {
    "wave": [("paper", 0.4), ("rock", 0.3), ("paper", 0.4),
              ("rock", 0.3), ("paper", 0.3)],
    "nod_like": [("thumbs_up", 0.5), ("release", 0.2), ("thumbs_up", 0.6)],
    "clench_open": [("rock", 0.7), ("paper", 0.7)],
    "count_five": [("fingers_1", 0.7), ("fingers_2", 0.7), ("fingers_3", 0.7),
                   ("fingers_4", 0.7), ("fingers_5", 0.8)],
    "comfort_rhythm": [("rock", 1.1), ("paper", 1.1), ("rock", 1.1),
                        ("paper", 1.1), ("fingers_5", 1.2)],
}

# The master-facing vocabulary uses ``gentle_hold`` while the hardware kit
# calls the same hand-2 sequence ``comfort_rhythm``.  Keep the alias here as
# well as at the HTTP boundary so a real RH56 backend cannot reject the
# canonical action after the request has already passed validation.
SEQUENCES["gentle_hold"] = SEQUENCES["comfort_rhythm"]

POSE_MAP = {
    "home": "release",
    "release": "release",
    "rps_rock": "rock",
    "rps_scissors": "scissors",
    "rps_paper": "paper",
    "sign_family_received": "thumbs_up",
    "finger_fist": "rock",
    "finger_open": "paper",
    "finger_count_1": "fingers_1",
    "finger_count_2": "fingers_2",
    "finger_count_3": "fingers_3",
    "finger_count_4": "fingers_4",
    "finger_count_5": "fingers_5",
    "fingers_1": "fingers_1",
    "fingers_2": "fingers_2",
    "fingers_3": "fingers_3",
    "fingers_4": "fingers_4",
    "fingers_5": "fingers_5",
    "rock": "rock",
    "paper": "paper",
    "scissors": "scissors",
    "thumbs_up": "thumbs_up",
    "hold_card": "hold_card",
    "grip_pillow": "rock",
    "ok": "thumbs_up",
}

# These names need the Panthera arm segment, a card, or an unverified hand
# pose.  Keep them explicit so a real-hand-only run never pretends to finish
# the complete rescue action.
ARM_REQUIRED = {
    "press_sos", "deliver_medicine", "sign_coming",
    "finger_pinch_1", "finger_pinch_2", "finger_pinch_3", "finger_pinch_4",
}


class RH56Backend:
    def __init__(self, hand1, hand2):
        self.hands = {1: hand1, 2: hand2}

    def execute(self, pose, hand):
        if pose in SEQUENCES:
            # The handoff defines this as a hand#2 rhythm, but the complete
            # rescue chain also needs the arm.  Run it only when explicitly
            # called as a hand-only pose by the operator.
            hand = 2 if pose in ("comfort_rhythm", "gentle_hold") else hand
            for name, hold in SEQUENCES[pose]:
                self.hands[hand].move(POSE_VOCAB[name])
                time.sleep(hold)
            return {"ok": True, "did": "seq:%s" % pose, "hand": hand,
                    "backend": "rh56_serial"}
        if pose in ARM_REQUIRED:
            return {"ok": False, "error": "%s requires Panthera arm backend" % pose,
                    "backend": "rh56_serial"}
        mapped = POSE_MAP.get(pose)
        if mapped is None or mapped not in POSE_VOCAB:
            return {"ok": False, "error": "no verified RH56 pose for %s" % pose,
                    "backend": "rh56_serial"}
        if hand not in self.hands:
            return {"ok": False, "error": "unknown hand %s" % hand,
                    "backend": "rh56_serial"}
        self.hands[hand].move(POSE_VOCAB[mapped])
        return {"ok": True, "did": "pose:" + mapped, "hand": hand,
                "backend": "rh56_serial"}

    def status(self):
        return {"hand1": self.hands[1].status(), "hand2": self.hands[2].status()}
