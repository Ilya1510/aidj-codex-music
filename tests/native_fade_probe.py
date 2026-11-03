from pathlib import Path
import subprocess
import tempfile

def main():
    root=Path(__file__).resolve().parents[1];jdk=Path.home()/'.local/share/aidj-codex-music/android-tools/jdk17/bin'
    with tempfile.TemporaryDirectory(prefix='aidj-fade-') as folder:
        subprocess.run([str(jdk/'javac'),'--release','8','-d',folder,str(root/'android/src/com/aidj/music/PlaybackFade.java'),str(root/'tests/PlaybackFadeProbe.java')],check=True)
        subprocess.run([str(jdk/'java'),'-Xmx64m','-XX:ActiveProcessorCount=2','-cp',folder,'com.aidj.music.PlaybackFadeProbe'],check=True)

if __name__=='__main__':main()
