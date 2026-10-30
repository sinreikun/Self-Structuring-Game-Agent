from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="自己発達ゲームAI")
    parser.add_argument("--apply-update", type=Path)
    parser.add_argument("--health-marker", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument('--io-self-test', action='store_true')
    parser.add_argument('--test-window-exe', type=Path)
    parser.add_argument('--test-window-script', type=Path)
    parser.add_argument('--self-test-report', type=Path)
    args = parser.parse_args()
    if args.apply_update:
        from .update import apply_update
        apply_update(args.apply_update)
        return
    if args.self_test:
        from .smoke import smoke
        smoke()
        return
    if args.io_self_test:
        if not all((args.test_window_exe,args.test_window_script,args.self_test_report)):
            parser.error('Window self-test requires executable, script and report paths')
        from .smoke import window_smoke
        window_smoke(args.test_window_exe,args.test_window_script,args.self_test_report)
        return
    import tkinter as tk
    from .gui import App
    root = tk.Tk()
    App(root, args.health_marker)
    root.mainloop()


if __name__ == "__main__":
    main()
