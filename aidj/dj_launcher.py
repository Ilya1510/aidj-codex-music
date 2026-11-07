"""Run the full-library controller; isolate heavy analysis in the CPU environment."""
import os
from pathlib import Path
import sys
from .audio import ffmpeg


def main():
    root=Path(__file__).resolve().parent.parent
    os.execv(sys.executable,[sys.executable,'-m','aidj.dj_batch','serve','--analysis-python',str(root/'.dj-venv/bin/python'),'--ffmpeg',ffmpeg(),'--threads','4'])


if __name__=='__main__':main()
