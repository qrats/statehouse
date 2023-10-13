"""Scrapy-facing layer.

Importing this package does not import Scrapy: the spiders and middlewares
that need it live in submodules and are loaded by the Scrapy runner. The
throttle and the frontier are plain Python and are used by the browser fetcher
too.
"""

from statehouse.scraping.frontier import CrawlFrontier, FrontierEntry
from statehouse.scraping.throttle import DomainThrottle, TokenBucket

__all__ = ["CrawlFrontier", "FrontierEntry", "DomainThrottle", "TokenBucket"]
