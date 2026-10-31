"""Run from source or the portable executable; keep data beside this launcher."""

from pathlib import Path
import sys


def main():
    if "--smoke-test" in sys.argv:
        from smoke_test import run

        try:
            run()
        except Exception:
            if "--smoke-output" in sys.argv:
                import json
                import traceback

                output = Path(sys.argv[sys.argv.index("--smoke-output") + 1])
                output.write_text(
                    json.dumps(
                        {"result": "failed", "traceback": traceback.format_exc()},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            raise
        return
    directory = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).parent
    )
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


if __name__ == "__main__":
    main()
