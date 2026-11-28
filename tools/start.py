"""Start the application and its local managed services."""
from pathlib import Path
import shutil
import subprocess

from configure import configure


root=Path(__file__).resolve().parents[1]
if not shutil.which('docker'):
    raise SystemExit('Docker Compose v2 is required. Install Docker and run this command again.')
subprocess.run(['docker','compose','version'],cwd=root,check=True,stdout=subprocess.DEVNULL)
configure(root)
subprocess.run(['docker','compose','up','-d','--build'],cwd=root,check=True)
print('Website, AList and Sonarr started.')
