"""Driver construction.

Isolated in one module so that the rest of the browser layer can be tested
with a fake driver and never imports Selenium.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from statehouse.config.settings import Settings

__all__ = ["DriverOptions", "chrome_arguments", "build_driver"]


@dataclass
class DriverOptions:
    """Everything we set on a Chrome instance."""

    headless: bool = True
    window_size: tuple[int, int] = (1440, 900)
    page_load_timeout: float = 45.0
    script_timeout: float = 30.0
    user_agent: str = ""
    download_dir: str = ""
    disable_images: bool = True
    remote_url: str = ""
    extra_arguments: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.page_load_timeout <= 0:
            raise ValueError("page_load_timeout must be positive")
        if self.script_timeout <= 0:
            raise ValueError("script_timeout must be positive")
        width, height = self.window_size
        if width <= 0 or height <= 0:
            raise ValueError("window_size must be positive")

    @classmethod
    def from_settings(cls, settings: Settings) -> "DriverOptions":
        return cls(
            headless=settings.browser_headless,
            page_load_timeout=settings.browser_page_load_timeout,
            user_agent=settings.user_agent,
            remote_url=settings.remote_webdriver_url,
        )


def chrome_arguments(options: DriverOptions) -> list[str]:
    """The argument list for a Chrome instance.

    Pulled out as a pure function because it is the part that actually goes
    wrong — a missing ``--no-sandbox`` inside a container fails at run time
    with an unhelpful message, and that is worth a test.
    """
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-popup-blocking",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={options.window_size[0]},{options.window_size[1]}",
    ]
    if options.headless:
        args.append("--headless=new")
    if options.disable_images:
        args.append("--blink-settings=imagesEnabled=false")
    if options.user_agent:
        args.append(f"--user-agent={options.user_agent}")
    args.extend(options.extra_arguments)
    return args


def build_driver(options: DriverOptions) -> Any:  # pragma: no cover - needs Selenium
    """Construct a Chrome driver, local or remote."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    chrome_options = Options()
    for argument in chrome_arguments(options):
        chrome_options.add_argument(argument)
    if options.download_dir:
        chrome_options.add_experimental_option(
            "prefs", {"download.default_directory": options.download_dir}
        )

    if options.remote_url:
        driver = webdriver.Remote(command_executor=options.remote_url, options=chrome_options)
    else:
        driver = webdriver.Chrome(options=chrome_options)

    driver.set_page_load_timeout(options.page_load_timeout)
    driver.set_script_timeout(options.script_timeout)
    return driver
