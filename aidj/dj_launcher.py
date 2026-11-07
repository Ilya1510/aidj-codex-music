"""Locate FFmpeg in the normal environment, then exec the isolated CPU worker."""
import os
from pathlib import Path
from .audio import ffmpeg


def main():
    root=Path(__file__).resolve().parent.parent
    os.execv(str(root/'.dj-venv/bin/python'),[str(root/'.dj-venv/bin/python'),'-m','aidj.dj_worker','serve','--ffmpeg',ffmpeg()])


if __name__=='__main__':main()
