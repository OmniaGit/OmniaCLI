"""Run the command line from a checkout, e.g. from an IDE launch configuration.

Running omniaci/cli.py directly breaks its relative imports; this script
runs it as part of the omniaci package. Installed copies use the omniaci command.
"""

import sys

from omniaci.cli import main

if __name__ == "__main__":
    sys.exit(main())
