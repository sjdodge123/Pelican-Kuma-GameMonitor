import sys
from pathlib import Path

# The app uses flat imports (cwd /app/app in the container); mirror that here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
