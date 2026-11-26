"""Install the Mac application responsible for only desktop and course folder access."""
import os
import plistlib
import subprocess
from pathlib import Path
from db import APP, STATE

def main():
    os.umask(0o077)
    bundle=APP/"Research Ideas Folder Sync.app"
    contents=bundle/"Contents"
    executable=contents/"MacOS/ResearchIdeasFolderSync"
    executable.parent.mkdir(parents=True,exist_ok=True)
    if not executable.exists():
        with (contents/"Info.plist").open("wb") as f:
            plistlib.dump({
                "CFBundleIdentifier":"org.researchideas.local.folders",
                "CFBundleName":"Research Ideas Folder Sync",
                "CFBundleDisplayName":"研究思想库文件同步",
                "CFBundleExecutable":executable.name,
                "CFBundlePackageType":"APPL",
                "CFBundleShortVersionString":"1.0",
                "CFBundleVersion":"1",
                "LSUIElement":True,
                "NSDesktopFolderUsageDescription":"只读索引你的桌面科研项目和 idea 文件夹，并更新研究思想库。",
                "NSDocumentsFolderUsageDescription":"只读索引 Documents 内列明的科研和课程资料。"
            },f)
        subprocess.run(["/usr/bin/swiftc","-O",str(APP/"FolderSync.swift"),"-o",str(executable)],check=True)
        subprocess.run(["/usr/bin/codesign","--force","--sign","-",str(bundle)],check=True)
    label="org.researchideas.local.folder-app"
    job=Path.home()/"Library/LaunchAgents"/(label+".plist")
    if not job.exists():
        with job.open("xb") as f:
            plistlib.dump({"Label":label,"ProgramArguments":["/usr/bin/open","-gj","-a",str(bundle)],"RunAtLoad":True,
                           "StartInterval":900,"ProcessType":"Background","StandardOutPath":str(STATE/"folder-launch.log"),
                           "StandardErrorPath":str(STATE/"folder-launch.log"),"Umask":0o077},f)
    domain=f"gui/{os.getuid()}"
    status=subprocess.run(["/bin/launchctl","print",f"{domain}/{label}"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if status.returncode:
        subprocess.run(["/bin/launchctl","bootstrap",domain,str(job)],check=True)
    subprocess.run(["/usr/bin/open","-gj","-a",str(bundle)],check=True)
    print(bundle)
if __name__=="__main__": main()
