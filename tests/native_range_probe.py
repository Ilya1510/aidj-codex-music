from pathlib import Path
import subprocess
import tempfile

def main():
    root=Path(__file__).resolve().parents[1];tools=Path.home()/'.local/share/aidj-codex-music/android-tools';jar=tools/'test-json/json.jar'
    with tempfile.TemporaryDirectory(prefix='aidj-range-') as folder:
        subprocess.run([str(tools/'jdk17/bin/javac'),'--release','8','-classpath',str(jar),'-d',folder,str(root/'android/src/com/aidj/music/PlaybackRange.java'),str(root/'tests/PlaybackRangeProbe.java')],check=True)
        subprocess.run([str(tools/'jdk17/bin/java'),'-Xmx64m','-XX:ActiveProcessorCount=2','-cp',folder+':'+str(jar),'com.aidj.music.PlaybackRangeProbe'],check=True)

if __name__=='__main__':main()
