"""Per-jurisdiction spiders.

One module per adapter named in the registry. The shared behaviour lives in
:mod:`statehouse.scraping.spiders.base`; a new state should be a subclass plus
a registry entry.
"""

from statehouse.scraping.spiders.base import JurisdictionSpider

__all__ = ["JurisdictionSpider"]
