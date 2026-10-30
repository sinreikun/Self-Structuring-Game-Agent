import sys

if __name__=='__main__':
    if sys.argv[1:2]==['--launcher-self-test']:
        from pathlib import Path
        import json
        import traceback
        report=Path(sys.argv[-1])
        try:
            report.write_text(json.dumps({'status':'running','stage':'launcher entrypoint','argument_count':len(sys.argv)}),encoding='utf-8')
            from selfstruct.launcher_smoke import smoke
            smoke(Path(sys.argv[2]),report)
        except BaseException:
            report.write_text(json.dumps({'status':'failed','error':traceback.format_exc()}),encoding='utf-8')
            sys.exit(1)
    else:
        from selfstruct.launch_runtime import main
        main()
