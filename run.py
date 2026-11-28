import sys
from pathlib import Path

dependencies=Path(__file__).resolve().parent/'.deps'
if dependencies.is_dir():sys.path.insert(0,str(dependencies))

if __name__=='__main__':
    if '--serve-child' in sys.argv:
        from mikan115.server import main
    else:
        from mikan115.supervisor import main
    main()
