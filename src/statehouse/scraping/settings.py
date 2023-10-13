"""Scrapy settings.

Read by the Scrapy runner via ``SCRAPY_SETTINGS_MODULE``. Values that differ
per environment come from :class:`~statehouse.config.settings.Settings` rather
than being hard-coded here.
"""

from __future__ import annotations

from statehouse.config.settings import load_settings

_settings = load_settings()

BOT_NAME = "statehouse"
SPIDER_MODULES = ["statehouse.scraping.spiders"]
NEWSPIDER_MODULE = "statehouse.scraping.spiders"

USER_AGENT = _settings.user_agent
ROBOTSTXT_OBEY = _settings.respect_robots

CONCURRENT_REQUESTS = _settings.default_concurrency * 4
CONCURRENT_REQUESTS_PER_DOMAIN = _settings.default_concurrency
DOWNLOAD_TIMEOUT = _settings.request_timeout_seconds
DOWNLOAD_DELAY = 60.0 / max(1, _settings.default_requests_per_minute)
RANDOMIZE_DOWNLOAD_DELAY = True

RETRY_ENABLED = True
RETRY_TIMES = _settings.max_retries
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 522, 524, 408]

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0

# Our own throttle is authoritative; Scrapy's is a second line of defence.
DOWNLOADER_MIDDLEWARES = {
    "statehouse.scraping.middlewares.throttle.PolitenessMiddleware": 300,
    "statehouse.scraping.middlewares.headers.RotatingHeadersMiddleware": 400,
    "statehouse.scraping.middlewares.retry.ClassifyingRetryMiddleware": 550,
    "statehouse.scraping.middlewares.archive.RawArchiveMiddleware": 900,
}

ITEM_PIPELINES = {
    "statehouse.scraping.pipelines.normalise.NormalisePipeline": 100,
    "statehouse.scraping.pipelines.quality.QualityPipeline": 200,
    "statehouse.scraping.pipelines.persist.PersistPipeline": 300,
}

HTTPCACHE_ENABLED = not _settings.is_production
HTTPCACHE_EXPIRATION_SECS = 3600
HTTPCACHE_DIR = "httpcache"
HTTPCACHE_IGNORE_HTTP_CODES = [429, 500, 502, 503, 504]

LOG_LEVEL = _settings.log_level
LOG_FORMATTER = "statehouse.observability.logging.ScrapyLogFormatter"

TELNETCONSOLE_ENABLED = False
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

DEPTH_LIMIT = 6
DEPTH_PRIORITY = 1
SCHEDULER_DISK_QUEUE = "scrapy.squeues.PickleFifoDiskQueue"
SCHEDULER_MEMORY_QUEUE = "scrapy.squeues.FifoMemoryQueue"
