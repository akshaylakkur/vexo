import os
import sys

os.environ["STEADY_NO_LOCAL"] = "1"      # tests never depend on this machine's config/local.yaml

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
