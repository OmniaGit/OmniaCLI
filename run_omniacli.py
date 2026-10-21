"""Run the command line from a checkout, e.g. from an IDE launch configuration.

Running omniacli/cli.py directly breaks its relative imports; this script
runs it as part of the omniacli package. Installed copies use the omniacli command.
"""

import sys

from omniacli.cli import main

if __name__ == "__main__":
    sys.exit(main())
