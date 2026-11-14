"""Make the Vexo Open kit's library and helpers importable. Import this before vexo_open."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # the project folder (holds vexo-open-kit/)
KIT = ROOT / "vexo-open-kit"

for sub in (KIT / "python", KIT / "examples" / "python"):
    if str(sub) not in sys.path:
        sys.path.insert(0, str(sub))

if not (KIT / "python" / "vexo_open").is_dir():
    raise SystemExit(f"The Vexo Open kit was not found at {KIT}. It must sit inside the project folder.")
