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
    args = parser.parse_args()
    if args.apply_update:
        from .update import apply_update
        apply_update(args.apply_update)
        return
    if args.self_test:
        from .smoke import smoke
        smoke()
        return
    import tkinter as tk
    from .gui import App
    root = tk.Tk()
    App(root, args.health_marker)
    root.mainloop()


if __name__ == "__main__":
    main()
