"""Prepare complete audio in R2 once, without storing a whole file in /tmp."""

import hashlib
import time
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config as ClientConfig
from botocore.exceptions import BotoCoreError, ClientError

from podcast import hls
from podcast.soundcloud import HLS_TYPES, SourceDeadline, SourceError

PART_SIZE = 8 * 1024 * 1024


class R2Audio:
    def __init__(self, config, client=None):
        self.config = config
        origin = urlsplit(config.r2_public_base)
        if (not config.r2_bucket or not config.r2_account_id or not config.r2_access_key
                or not config.r2_secret_key or (config.r2_public_base and
                (origin.scheme != "https" or not origin.hostname or origin.username
                 or origin.password or origin.query or origin.fragment))):
            raise SourceError("R2 audio hosting is not configured")
        self.client = client or boto3.client(
            "s3", endpoint_url=f"https://{config.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=config.r2_access_key, aws_secret_access_key=config.r2_secret_key,
            region_name="auto", config=ClientConfig(
                connect_timeout=3, read_timeout=10, retries={"max_attempts": 1},
                request_checksum_calculation="when_required", response_checksum_validation="when_required"))

    def close(self):
        self.client.close()

    def key(self, track):
        identity = str(track.get("id") or track["webpage_url"])
        return self.config.r2_prefix + "/" + hashlib.sha256(identity.encode()).hexdigest() + ".m4a"

    def info(self, key, length):
        return {"delivery": "r2", "key": key, "length": length, "type": "audio/mp4"}

    def link(self, key, method="GET"):
        if not key.startswith(self.config.r2_prefix + "/") or ".." in key or method not in ("GET", "HEAD"):
            raise SourceError("Invalid stored audio identity")
        if self.config.r2_public_base:
            return self.config.r2_public_base + "/" + quote(key, safe="/")
        # HEAD and GET signatures differ. Sign for the original client method;
        # byte ranges are intentionally not part of the signed headers.
        return self.client.generate_presigned_url(
            "head_object" if method == "HEAD" else "get_object",
            Params={"Bucket": self.config.r2_bucket, "Key": key}, ExpiresIn=86400)

    def existing(self, key, expected_duration):
        try:
            obj = self.client.head_object(Bucket=self.config.r2_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        metadata = obj.get("Metadata", {})
        if (obj.get("ContentType") == "audio/mp4" and obj.get("ContentLength", 0) > 0
                and metadata.get("complete") == "1"
                and abs(float(metadata.get("duration", "0")) - expected_duration) <= max(2, expected_duration * .001)):
            return self.info(key, obj["ContentLength"])
        raise SourceError("Existing audio object failed validation")

    def prepare(self, track, source, deadline):
        """A completed upload is reusable after lost responses or Redis failures."""
        key = self.key(track)
        try:
            audio = self.existing(key, track.get("duration", 0))
            if audio:
                return audio
            audio = source.resolve_audio(track)
            if audio["type"] not in HLS_TYPES:
                return audio  # A genuine full MP3 can still be served by SoundCloud.
            indexed = hls.prepare(audio["url"], track.get("duration", 0), deadline=deadline)
            self.upload(key, indexed, deadline)
            result = self.existing(key, track.get("duration", 0))
            if not result or result["length"] != indexed["length"]:
                raise SourceError("Completed audio object failed verification")
            return result
        except (BotoCoreError, ClientError, hls.HLSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise SourceDeadline("Audio preparation budget exhausted") from exc
            raise SourceError("Audio storage preparation failed") from exc

    def upload(self, key, indexed, deadline):
        args = {"Bucket": self.config.r2_bucket, "Key": key}
        upload_id = None
        try:
            self.check_deadline(deadline)
            upload_id = self.client.create_multipart_upload(
                **args, ContentType="audio/mp4", CacheControl="public, max-age=86400",
                Metadata={"complete": "1", "duration": str(indexed["duration"])})["UploadId"]
            parts, buffer, length = [], bytearray(), 0

            def send(data):
                self.check_deadline(deadline)
                number = len(parts) + 1
                result = self.client.upload_part(**args, UploadId=upload_id, PartNumber=number, Body=data)
                parts.append({"PartNumber": number, "ETag": result["ETag"]})

            for chunk in hls.chunks(indexed, 0, indexed["length"] - 1, deadline=deadline):
                length += len(chunk)
                if length > indexed["length"]:
                    raise SourceError("Audio exceeded its expected size")
                buffer.extend(chunk)
                while len(buffer) >= PART_SIZE:
                    send(bytes(buffer[:PART_SIZE]))
                    del buffer[:PART_SIZE]
            if length != indexed["length"]:
                raise SourceError("Audio download was incomplete")
            if buffer:
                send(bytes(buffer))
            self.check_deadline(deadline)
            self.client.complete_multipart_upload(**args, UploadId=upload_id, MultipartUpload={"Parts": parts})
            upload_id = None
        finally:
            if upload_id:
                try:
                    self.client.abort_multipart_upload(**args, UploadId=upload_id)
                except (BotoCoreError, ClientError):
                    pass  # R2's incomplete-upload lifecycle is the final cleanup.

    @staticmethod
    def check_deadline(deadline):
        if time.monotonic() >= deadline:
            raise SourceDeadline("Audio preparation budget exhausted")
