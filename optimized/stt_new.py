"""Run from source or the portable executable; keep data beside this launcher."""
from pathlib import Path
import sys


def main():
    if '--qwen-action' in sys.argv:
        import json
        from qwen_backend import ToolkitBackend
        action = sys.argv[sys.argv.index('--qwen-action') + 1]
        output = Path(sys.argv[sys.argv.index('--qwen-output') + 1])
        try:
            if action not in ('status', 'start', 'stop'):
                raise ValueError('Invalid Qwen action')
            result = getattr(ToolkitBackend(), action)()
        except Exception as error:
            result = {'ok': False, 'error_type': type(error).__name__}
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    if '--smoke-test' in sys.argv:
        from smoke_test import run
        try:
            run()
        except Exception:
            if '--smoke-output' in sys.argv:
                import json
                import traceback
                output = Path(sys.argv[sys.argv.index('--smoke-output') + 1])
                output.write_text(json.dumps({'result': 'failed', 'traceback': traceback.format_exc()},
                                            ensure_ascii=False, indent=2), encoding='utf-8')
            raise
        return
    directory = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    from single_instance import SingleInstance, show_existing_instance
    instance = SingleInstance(directory)
    if not instance.acquire():
        show_existing_instance()
        return
    try:
        import tkinter as tk
        from app import CapsLockChecker
        root = tk.Tk()
        CapsLockChecker(root, directory=directory)
        root.mainloop()
    finally:
        instance.close()


if __name__ == '__main__':
    main()
