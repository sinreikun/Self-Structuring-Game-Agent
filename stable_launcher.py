import sys

if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--launcher-self-test':
        from pathlib import Path
        from selfstruct.launcher_smoke import smoke
        smoke(Path(sys.argv[2]),Path(sys.argv[3]))
    else:
        from selfstruct.launch_runtime import main
        main()
