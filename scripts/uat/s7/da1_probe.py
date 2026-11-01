"""S7: show exactly what the terminal under test answers to earthwave's own probe query (CSI 16t, CSI 14t, CSI c).

    python da1_probe.py OUT.json

Runs inside the terminal being tested (driven by run_wt.py). Uses the app's own `query_terminal` and
`parse_reply`, so the
bytes are what the app would have seen. The raw reply is written escaped, with the parsed verdict; nothing is forced.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

# Load the app's probe module straight from its file (stdlib only), so the driver's Python needs no earthwave install.
PROBE = Path(__file__).resolve().parents[3] / "earthwave" / "ui" / "graphics" / "probe.py"
spec = importlib.util.spec_from_file_location("ew_probe", PROBE)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
sys.modules["ew_probe"] = probe
spec.loader.exec_module(probe)

reply = probe.query_terminal()
size = tuple(os.get_terminal_size())
parsed = probe.parse_reply(reply, *size)
choice = probe.decide("auto", os.environ, True, size, lambda: reply)
record = {
    "raw_reply_escaped": reply.encode("unicode_escape").decode("ascii"),
    "terminal_size": size,
    "parsed": None if parsed is None else {"sixel": parsed.sixel, "cell": parsed.cell},
    "decision_auto": {"renderer": choice.renderer, "reason": choice.reason, "cell": choice.cell},
    "WT_SESSION": os.environ.get("WT_SESSION"),
    "TERM": os.environ.get("TERM"),
}
Path(sys.argv[1]).write_text(json.dumps(record, indent=2), encoding="utf-8")
print("DA1PROBE DONE", record["raw_reply_escaped"])
