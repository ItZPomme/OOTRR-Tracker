"""python -m ootrr_tracker [--ootrr PATH] [--out DIR] [--install]"""

import argparse
import sys

from . import use_ootrr

pre = argparse.ArgumentParser(add_help=False)
pre.add_argument("--ootrr")
use_ootrr(pre.parse_known_args()[0].ootrr)

from .poptracker import main  # noqa: E402  (after the OOTRR checkout is chosen)

sys.exit(main())
