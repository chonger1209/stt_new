"""Build reviewed release attachments without copying live configs or history."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}(?:\.\d+)?', args.version):
        parser.error('version must be YYYY.MM.DD[.revision]')
    repo = Path(__file__).resolve().parents[1]
    source = repo/'optimized'
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = subprocess.check_output(['git','ls-files','-z','optimized'], cwd=repo).decode().split('\0')
    files = [Path(name) for name in files if name]
    for name in files:
        if name.name == 'config.txt' or name.suffix in ('.db','.exe','.pyc') or any(p in ('logs','runtime','__pycache__') for p in name.parts):
            raise RuntimeError('Non-release tracked file: '+str(name))
    required = ['app.py','qwen_config.py','process_identity.py','config.default.txt','docs/QWEN_SETUP.md',
                '配套资源/千问管理工具包/scripts/manager.py','配套资源/千问管理工具包/service.json']
    for name in required:
        if Path('optimized')/name not in files:
            raise RuntimeError('Required file is not tracked: '+name)
    exe = output/f'stt_new-{args.version}-windows-x64.exe'
    portable = output/f'STT-{args.version}-windows-x64-portable.zip'
    archive = output/f'STT-{args.version}-source.zip'
    checksums = output/f'STT-{args.version}-SHA256SUMS.txt'
    for path in (exe,portable,archive,checksums):
        if path.exists():
            raise FileExistsError('Refusing to overwrite release artifact: '+str(path))
    shutil.copy2(args.exe,exe)
    with zipfile.ZipFile(portable,'w',zipfile.ZIP_DEFLATED) as z:
        prefix=f'STT-{args.version}/'
        z.write(exe,prefix+'stt_new.exe')
        for name,target in [('config.default.txt','config.txt'),('screen_time_tracker_new.ico','screen_time_tracker_new.ico'),('README.md','README.md')]:
            z.write(source/name,prefix+target)
        for name in files:
            relative=name.relative_to('optimized')
            if relative.parts[0] in ('docs','配套资源'):
                z.write(repo/name,prefix+relative.as_posix())
        z.write(source/'docs/QWEN_SETUP.md',prefix+'千问换机配置教程.md')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(repo/name,f'STT-{args.version}-source/'+name.relative_to('optimized').as_posix())
    assets=[exe,portable,archive]
    checksums.write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in assets),encoding='utf-8')
    for path in (portable,archive):
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                raise RuntimeError('ZIP verification failed')
    print('\n'.join(str(p) for p in [*assets,checksums]))


if __name__=='__main__':
    main()
