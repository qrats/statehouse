"""Pulling structure out of fetched bytes."""

from statehouse.extract.tables import HtmlTable, parse_tables
from statehouse.extract.textract import extract_body_text, guess_content_kind

__all__ = ["HtmlTable", "parse_tables", "extract_body_text", "guess_content_kind"]
