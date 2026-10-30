"""A launcher exits before its native child opens a window."""
from pathlib import Path
import subprocess
import sys

if __name__=='__main__':
    subprocess.Popen([sys.executable,str(Path(__file__).with_name('message_window.py'))])
