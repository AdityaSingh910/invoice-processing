"""Document storage abstraction for uploaded invoice PDFs.

WHAT THIS IS FOR

The database (see storage.py's `documents` table) holds METADATA about an
uploaded invoice -- original filename, MIME type, size, hash, who uploaded
it, when, and how -- plus an opaque `storage_key`. It never holds the PDF
bytes themselves. This module is the one place that reads or writes the
actual file content, behind a small interface (`DocumentStore`) so the rest
of the application never knows or cares whether a document lives on local
disk or in an S3 bucket.

Two implementations:

* `LocalDocumentStore` -- files under `config.DOCUMENT_STORAGE_DIR`. Needs
  nothing installed, nothing configured. This is what local development and
  the case-study demo actually use.
* `S3DocumentStore` -- an S3-compatible bucket, for a real deployment where
  the application may run on several instances or ephemeral containers with
  no shared local disk. `boto3` is imported lazily, inside the constructor,
  so a local-only install never needs the dependency at all.

WHY THE STORAGE KEY IS NEVER THE ORIGINAL FILENAME

The original filename is attacker-controlled input (see main.py's
`_safe_filename`, which already strips directory components and unsafe
characters before it ever reaches this module). Using it -- sanitised or
not -- as a filesystem path is still one missed edge case away from path
traversal or a collision between two different uploads that happen to share
a name. Every document gets a fresh, server-generated key
(`new_storage_key()`, a UUID4) instead; the original name is preserved only
as metadata for display and for the filename offered on download.
"""
import abc
import os
import re

import config

# A storage key this module ever generates or accepts always matches this
# shape exactly -- 32 lowercase hex characters (a UUID4 with no dashes) plus
# the fixed .pdf extension. Any other string is refused before it is ever
# joined onto a filesystem path or an object key, so a corrupted or
# maliciously-crafted `documents.storage_key` row can never be used to read
# or write outside where documents actually live.
_KEY_RE = re.compile(r"^[0-9a-f]{32}\.pdf$")


def new_storage_key() -> str:
    """A fresh, unguessable, filesystem-safe key. Never derived from anything
    the caller sent -- see module docstring."""
    import uuid
    return f"{uuid.uuid4().hex}.pdf"


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(f"invalid document storage key: {key!r}")
    return key


class DocumentStore(abc.ABC):
    """Content storage for invoice PDFs. Implementations must be safe to call
    with a key from `new_storage_key()` and refuse anything else."""

    @abc.abstractmethod
    def save(self, key: str, data: bytes) -> None:
        ...

    @abc.abstractmethod
    def read(self, key: str) -> bytes:
        ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        ...


class LocalDocumentStore(DocumentStore):
    """Files on local disk, under `config.DOCUMENT_STORAGE_DIR`.

    Every path this class ever touches is built from a key that has already
    passed `_validate_key` -- fixed shape, no separators, no `..` -- so there
    is no traversal surface here even before considering that the key is
    always server-generated, never user input.
    """

    def __init__(self, root: str = None):
        self.root = os.path.abspath(root or config.DOCUMENT_STORAGE_DIR)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        key = _validate_key(key)
        path = os.path.join(self.root, key)
        # Belt and braces: confirm the resolved path still sits inside the
        # storage root, exactly the same defence used for sample-invoice
        # serving in main.py (bug #12 in CLAUDE.md was this exact class of
        # mistake with an unvalidated name).
        if os.path.commonpath([self.root, os.path.abspath(path)]) != self.root:
            raise ValueError(f"invalid document storage key: {key!r}")
        return path

    def save(self, key: str, data: bytes) -> None:
        if len(data) > config.MAX_UPLOAD_BYTES:
            raise ValueError("document exceeds the configured upload limit")
        path = self._path(key)
        # Write to a temp file in the same directory, then atomically rename
        # into place -- a reader can never observe a partially-written file.
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def read(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def exists(self, key: str) -> bool:
        try:
            return os.path.isfile(self._path(key))
        except ValueError:
            return False

    def delete(self, key: str) -> None:
        path = self._path(key)
        if os.path.isfile(path):
            os.remove(path)


class S3DocumentStore(DocumentStore):
    """An S3-compatible bucket. `boto3` is imported lazily in __init__, so a
    local-only install (the default, `DOCUMENT_STORE_BACKEND=local`) never
    needs the package -- it is not in requirements.txt for that reason."""

    def __init__(self, bucket: str = None, prefix: str = None,
                region: str = None, endpoint_url: str = None):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "DOCUMENT_STORE_BACKEND=s3 requires the boto3 package "
                "('pip install boto3'), which is not installed."
            ) from exc
        self.bucket = bucket or config.document_s3_bucket()
        if not self.bucket:
            raise RuntimeError(
                "DOCUMENT_STORE_BACKEND=s3 requires DOCUMENT_S3_BUCKET to be set.")
        self.prefix = prefix if prefix is not None else config.document_s3_prefix()
        self._client = boto3.client(
            "s3",
            region_name=region or config.document_s3_region(),
            endpoint_url=endpoint_url or config.document_s3_endpoint_url(),
        )

    def _object_key(self, key: str) -> str:
        key = _validate_key(key)
        return f"{self.prefix}{key}" if self.prefix else key

    def save(self, key: str, data: bytes) -> None:
        if len(data) > config.MAX_UPLOAD_BYTES:
            raise ValueError("document exceeds the configured upload limit")
        self._client.put_object(Bucket=self.bucket, Key=self._object_key(key),
                                Body=data, ContentType="application/pdf")

    def read(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(key))


def get_store() -> DocumentStore:
    """The active document store, chosen by `DOCUMENT_STORE_BACKEND`.

    Not cached at module level: config is read at call time throughout this
    codebase (see config.py's own comments on this) so a value changed in
    .env or the environment mid-process -- which only ever happens in tests
    -- takes effect on the next call rather than needing a restart.
    """
    if config.document_store_backend() == "s3":
        return S3DocumentStore()
    return LocalDocumentStore()
