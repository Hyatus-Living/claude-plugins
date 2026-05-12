#!/usr/bin/env python3

import sys

from thread_finder_core import main


if __name__ == "__main__":
    sys.argv.insert(1, "index")
    main()
