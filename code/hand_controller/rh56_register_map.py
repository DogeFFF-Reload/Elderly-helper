"""RH56F2 Modbus register map.

The addresses below come from the hardware handoff and are intentionally
guarded by both ``PROVISIONAL`` and ``RH56_REGMAP_CONFIRMED``.  The handoff
marks them as provisional; real writes stay disabled until the team checks
the vendor manual on the actual hand.  After the check, update this one file,
set ``PROVISIONAL = False``, and set the environment flag on the hardware
machine.
"""

SLAVE_ID_HAND1 = 1
SLAVE_ID_HAND2 = 2
BAUD = 115200
PARITY = "N"
STOPBITS = 1
BYTESIZE = 8

# Provisional addresses from the hardware team's 2026-10-24 handoff.
REG_TARGET_BASE = 0x0100       # six target joints, FC16
REG_SPEED = 0x0106             # target speed, FC06 (optional)
REG_STATUS = 0x0200            # status/error, FC03
REG_CONTROL = 0x0210           # enable/control word, FC06 (optional)

VALUE_MIN = 0
VALUE_MAX = 1000
SCALE = 100.0                  # internal percentage range is 0..100
JOINT_ORDER = ["thumb", "index", "middle", "ring", "pinky", "thumb_rot"]

# Keep this false until the four-address checklist in docs/10 is checked
# against the exact RH56F2 firmware in the team's hands.
PROVISIONAL = True


def confirmed_from_environment(value=None):
    """Return whether it is safe to send a real write to the hand.

    The explicit argument is useful for tests.  Production defaults to false
    so setting ``MOCK_SERIAL=0`` alone cannot accidentally write through a
    still-unverified map.
    """
    if value is not None:
        return bool(value)
    import os
    return os.environ.get("RH56_REGMAP_CONFIRMED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
