__VERSION__ = "0.0.1"
__AUTHOR__ = "tianxiu2b2t"
__NAME__ = "Example Plugin"

from core import logger
from core import cluster


async def enable():
    if not cluster.cluster:
        logger.error("Current not any cluster?")
        return
    logger.info("This is a Example Plugin")