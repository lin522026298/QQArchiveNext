"""Local, authenticated NTQQ capture and append-only archive, format 1."""
__version__ = "0.3.0"
from pathlib import Path
import sys

# Source checkout bootstrap; frozen builds resolve packaged modules normally.
_viewer=Path(__file__).resolve().parents[1]/'viewer/qq_nt_archive_viewer/src'
if _viewer.is_dir() and str(_viewer) not in sys.path:sys.path.insert(0,str(_viewer))
