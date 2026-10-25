import json
from pathlib import Path
import sys
import traceback
from ..captures.jobs import atomic_json
from .pipeline import ScanOptions,process_scan

def main():
    inputs,work=Path(sys.argv[1]),Path(sys.argv[2]);options=ScanOptions(**json.loads(sys.argv[3]))
    def update(state,progress,message):atomic_json(work/'worker-progress.json',{'state':state,'progress':progress,'message':message})
    try:process_scan(inputs,work,update,lambda:None,options=options)
    except BaseException as exc:
        atomic_json(work/'worker-error.json',{'error':str(exc),'type':type(exc).__name__});traceback.print_exc();return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
