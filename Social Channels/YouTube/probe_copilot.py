import subprocess
import json

def probe():
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", 
         """
         $m = Get-AppxPackageManifest (Get-AppxPackage Microsoft.Copilot)
         $app = $m.Package.Applications.Application
         Write-Output "App ID: $($app.Id)"
         Write-Output "StartPage: $($app.StartPage)"
         Write-Output "Extensions: $($app.Extensions.OuterXml)"
         """],
        capture_output=True, text=True
    )
    print("Copilot App details:")
    print(res.stdout)

if __name__ == "__main__":
    probe()

