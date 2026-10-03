"""Every module that adds `swarm` subcommands. Each has `register(sub)`; help lists them in this order.

To add a feature: write lib/tstack/<feature>.py with register(sub), import it here, add it to MODULES.
The reserved lines below are kept apart by blank lines so parallel branches merge without conflicts.
"""

from . import config, managers, messaging, tasks

from . import server

from . import night, routine

from . import slack

from . import upkeep

MODULES = [
    tasks,
    managers,
    messaging,
    config,

    server,

    routine,
    night,

    slack,

    upkeep,
]
