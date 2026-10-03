"""swarm: memory notes and a manager/worker swarm of coding agents in herdr.

Tasks live as folders on disk. Managers are long-lived agents, one herdr workspace each. Workers
are short-lived agents in the manager's `workers` tab, one per task. Memory lives in a separate git
repo of notes. Settings: $TSTACK_CONFIG, else ~/.config/tstack/config.json.

Run `swarm <command> -h` for a command's options.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .commands import MODULES
from .util import SwarmError


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="swarm", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    for module in MODULES:
        module.register(sub)
    return top


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.run(args) or 0)
    except SwarmError as error:
        print(f"swarm: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
