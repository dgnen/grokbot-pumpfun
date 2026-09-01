"""Put the project root on sys.path so tests can import the `src` package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
