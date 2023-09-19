"""Transform: portal-shaped records in, canonical records out."""

from statehouse.transform.diffing import DocumentDiff, diff_documents
from statehouse.transform.normalize import normalise_document
from statehouse.transform.status import classify_action, derive_status, status_rank

__all__ = [
    "DocumentDiff",
    "diff_documents",
    "normalise_document",
    "classify_action",
    "derive_status",
    "status_rank",
]
