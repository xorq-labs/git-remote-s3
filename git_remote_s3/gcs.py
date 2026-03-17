# SPDX-FileCopyrightText: 2024-present
#
# SPDX-License-Identifier: Apache-2.0

"""GCS-native storage client with a boto3-S3-compatible interface.

Translates google-cloud-storage calls and exceptions into the same shapes
that the rest of git-remote-s3 expects from boto3, so the calling code in
remote.py and manage.py can remain unchanged.

The critical difference: put_object with IfNoneMatch="*" is translated to
GCS's ``if_generation_match=0``, which *is* enforced by GCS (unlike the
S3-compat XML API, which silently ignores If-None-Match on writes).
"""

import io
import logging

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

try:
    from google.cloud import storage as _gcs_storage
    from google.api_core import exceptions as _gcs_exc
except ImportError:
    _gcs_storage = None
    _gcs_exc = None


def _require_gcs_sdk():
    if _gcs_storage is None:
        raise ImportError(
            "google-cloud-storage is required for gcs:// remotes. "
            "Install it with: pip install 'git-remote-s3[gcs]'"
        )


def _client_error(code, message, http_status):
    """Build a botocore ClientError so existing except-blocks keep working."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        },
        "GCSOperation",
    )


def _translate_error(exc):
    """Re-raise a google-api-core exception as a botocore ClientError."""
    if isinstance(exc, ClientError):
        raise
    if _gcs_exc is not None:
        if isinstance(exc, _gcs_exc.NotFound):
            raise _client_error("NoSuchKey", str(exc), 404) from exc
        if isinstance(exc, _gcs_exc.Forbidden):
            raise _client_error("AccessDenied", str(exc), 403) from exc
        if isinstance(exc, _gcs_exc.PreconditionFailed):
            raise _client_error("PreconditionFailed", str(exc), 412) from exc
    raise


class GCSClient:
    """Drop-in replacement for a boto3 S3 client backed by google-cloud-storage.

    Only the S3 methods actually used by git-remote-s3 are implemented.
    """

    def __init__(self):
        _require_gcs_sdk()
        self._client = _gcs_storage.Client()

    # ------------------------------------------------------------------
    # list_objects_v2
    # ------------------------------------------------------------------
    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        try:
            blobs = list(self._client.list_blobs(Bucket, prefix=Prefix))
            contents = [
                {"Key": b.name, "LastModified": b.updated} for b in blobs
            ]
            # No ContinuationToken — list_blobs auto-paginates.
            return {"Contents": contents}
        except Exception as exc:
            # NotFound here means the *bucket* doesn't exist.
            if _gcs_exc is not None and isinstance(exc, _gcs_exc.NotFound):
                raise _client_error("NoSuchBucket", str(exc), 404) from exc
            _translate_error(exc)

    # ------------------------------------------------------------------
    # put_object  (handles IfNoneMatch="*" → if_generation_match=0)
    # ------------------------------------------------------------------
    def put_object(
        self,
        *,
        Bucket,
        Key,
        Body=b"",
        IfNoneMatch=None,
        Metadata=None,
        ContentDisposition=None,
    ):
        try:
            blob = self._client.bucket(Bucket).blob(Key)
            upload_kwargs = {}
            if IfNoneMatch == "*":
                upload_kwargs["if_generation_match"] = 0
            if ContentDisposition:
                blob.content_disposition = ContentDisposition
            if Metadata:
                blob.metadata = Metadata
            if isinstance(Body, (bytes, str)):
                data = Body if isinstance(Body, bytes) else Body.encode("utf-8")
                blob.upload_from_string(data, **upload_kwargs)
            else:
                # file-like object
                blob.upload_from_file(Body, rewind=True, **upload_kwargs)
        except Exception as exc:
            _translate_error(exc)

    # ------------------------------------------------------------------
    # get_object
    # ------------------------------------------------------------------
    def get_object(self, *, Bucket, Key):
        try:
            blob = self._client.bucket(Bucket).blob(Key)
            content = blob.download_as_bytes()
            return {"Body": io.BytesIO(content)}
        except Exception as exc:
            _translate_error(exc)

    # ------------------------------------------------------------------
    # head_object
    # ------------------------------------------------------------------
    def head_object(self, *, Bucket, Key):
        try:
            blob = self._client.bucket(Bucket).blob(Key)
            blob.reload()
            return {"LastModified": blob.updated}
        except Exception as exc:
            _translate_error(exc)

    # ------------------------------------------------------------------
    # delete_object  (ignores NotFound, matching S3 behavior)
    # ------------------------------------------------------------------
    def delete_object(self, *, Bucket, Key):
        try:
            blob = self._client.bucket(Bucket).blob(Key)
            blob.delete()
        except Exception as exc:
            if _gcs_exc is not None and isinstance(exc, _gcs_exc.NotFound):
                return
            _translate_error(exc)

    # ------------------------------------------------------------------
    # download_file  (Config is accepted but ignored; GCS handles chunking)
    # ------------------------------------------------------------------
    def download_file(self, *, Bucket, Key, Filename, Config=None):
        try:
            blob = self._client.bucket(Bucket).blob(Key)
            blob.download_to_filename(Filename)
        except Exception as exc:
            _translate_error(exc)

    # ------------------------------------------------------------------
    # copy_object  (used by the doctor's fix_multiple_bundles)
    # ------------------------------------------------------------------
    def copy_object(self, *, CopySource, Bucket, Key):
        try:
            src_bucket = self._client.bucket(CopySource["Bucket"])
            src_blob = src_bucket.blob(CopySource["Key"])
            dst_bucket = self._client.bucket(Bucket)
            src_bucket.copy_blob(src_blob, dst_bucket, Key)
        except Exception as exc:
            _translate_error(exc)
