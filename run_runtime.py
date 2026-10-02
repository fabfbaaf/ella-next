"""PyCharm-friendly entry point for the local runtime."""

import multiprocessing

from ella_runtime.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
