from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
        '--onefile', '--windowed', '--name', 'stt_new',
        '--icon', str(root / 'screen_time_tracker_new.ico'),
        '--hidden-import', 'argparse', '--hidden-import', 'urllib.request',
        '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'),
        '--specpath', str(root / 'build'), str(root / 'stt_new.py')], cwd=root, check=True)
