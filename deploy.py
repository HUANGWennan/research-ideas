"""Configure restartable, per-user launchd jobs and a local shortcut."""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from db import APP, ROOT, STATE

PYTHON="/usr/bin/python3"
LAUNCHCTL="/bin/launchctl"
LABEL="org.researchideas.local"
DOMAIN=f"gui/{os.getuid()}"

def plist(label,args,interval=None):
    payload={"Label":label,"ProgramArguments":[PYTHON,"-B",str(APP/args[0]),*args[1:]],
             "RunAtLoad":True,"WorkingDirectory":str(APP),
             "StandardOutPath":str(STATE/("web.log" if label.endswith(".web") else "worker.log")),
             "StandardErrorPath":str(STATE/("web.log" if label.endswith(".web") else "worker.log")),
             "ProcessType":"Background","ThrottleInterval":15,"Umask":0o077}
    if interval:
        payload["StartInterval"]=interval
        payload["Nice"]=8
    else:
        payload["KeepAlive"]=True
    return payload

def launchctl(*args,check=True):
    return subprocess.run([LAUNCHCTL,*args],check=check,capture_output=True,text=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("install","stop","status"))
    args=parser.parse_args()
    os.umask(0o077)
    jobs=[("web",["web.py"],None),("sync",["maintenance.py","run"],300),
          ("embedding",["maintenance.py","vectors","480"],300)]
    directory=Path.home()/"Library/LaunchAgents"
    if args.action=="install":
        STATE.mkdir(exist_ok=True,parents=True)
        directory.mkdir(exist_ok=True)
        shortcut=ROOT/"打开研究思想库.webloc"
        if not shortcut.exists():
            with shortcut.open("xb") as f: plistlib.dump({"URL":"http://127.0.0.1:8767/"},f)
    for suffix,program,interval in jobs:
        label=f"{LABEL}.{suffix}"
        target=f"{DOMAIN}/{label}"
        path=directory/(label+".plist")
        desired=plist(label,program,interval)
        status=launchctl("print",target,check=False)
        if args.action=="status":
            print(label, "已加载" if status.returncode==0 else "未加载")
            for line in status.stdout.splitlines():
                if "state =" in line or "runs =" in line or "last exit code =" in line:
                    print(line.strip())
        elif args.action=="stop":
            if status.returncode==0: launchctl("bootout",target)
        elif args.action=="install":
            if path.exists() and plistlib.loads(path.read_bytes())!=desired:
                raise RuntimeError(f"配置已存在且内容不同：{path}")
            if not path.exists():
                with path.open("xb") as f: plistlib.dump(desired,f)
            if status.returncode:
                result=launchctl("bootstrap",DOMAIN,str(path),check=False)
                if result.returncode: raise RuntimeError(result.stderr.strip())
            print(label,"已加载")
if __name__=="__main__": main()
