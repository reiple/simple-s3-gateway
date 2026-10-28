#!/usr/bin/env python3
"""boto3 compatibility smoke tests for the read-only S3 gateway.

Requires boto3 (skipped if missing). Same env vars as test_s3_gateway.py:

    S3_ENDPOINT=http://localhost:9020 S3_DATA_DIR=../data python3 -m unittest -v test_boto3
"""
import os
import shutil
import unittest
import uuid

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    boto3 = None

ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9020")
DATA_DIR = os.path.abspath(os.environ.get(
    "S3_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")))
BUCKET = "boto-" + uuid.uuid4().hex[:12]
FILES = {
    "root.txt": b"root",
    "docs/f1.txt": b"n1",
    "docs/f2.txt": b"n2",
    "docs/f3.txt": b"n3",
    "docs/sub/z.txt": b"zzz",
    "한글/파일.txt": "유니코드".encode(),
}


@unittest.skipIf(boto3 is None, "boto3 not installed")
class Boto3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(DATA_DIR):
            raise unittest.SkipTest("data dir not found: %s (set S3_DATA_DIR)" % DATA_DIR)
        cls.bucket_dir = os.path.join(DATA_DIR, BUCKET)
        for key, content in FILES.items():
            path = os.path.join(cls.bucket_dir, *key.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        # Credentials are not verified by the gateway; path-style is required.
        cls.s3 = boto3.client(
            "s3", endpoint_url=ENDPOINT, region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="y",
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "bucket_dir", "/nonexistent-skip"), ignore_errors=True)

    def list(self, **kw):
        r = self.s3.list_objects_v2(Bucket=BUCKET, **kw)
        return r, [o["Key"] for o in r.get("Contents", [])], \
            [p["Prefix"] for p in r.get("CommonPrefixes", [])]

    def test_list_all(self):
        r, keys, prefixes = self.list()
        self.assertEqual(keys, sorted(FILES))
        self.assertEqual(prefixes, [])
        self.assertEqual(r["KeyCount"], len(FILES))
        self.assertFalse(r["IsTruncated"])

    def test_list_contents_fields(self):
        r, _, _ = self.list(Prefix="docs/sub/")
        obj = r["Contents"][0]
        self.assertEqual(obj["Size"], 3)
        self.assertEqual(obj["StorageClass"], "STANDARD")
        self.assertIsNotNone(obj["LastModified"].tzinfo)

    def test_list_delimiter(self):
        _, keys, prefixes = self.list(Delimiter="/")
        self.assertEqual(keys, ["root.txt"])
        self.assertEqual(prefixes, ["docs/", "한글/"])

    def test_list_prefix_delimiter(self):
        _, keys, prefixes = self.list(Prefix="docs/", Delimiter="/")
        self.assertEqual(keys, ["docs/f1.txt", "docs/f2.txt", "docs/f3.txt"])
        self.assertEqual(prefixes, ["docs/sub/"])

    def test_list_unicode_prefix(self):
        _, keys, _ = self.list(Prefix="한글/")
        self.assertEqual(keys, ["한글/파일.txt"])

    def test_max_keys_truncated(self):
        r, keys, _ = self.list(MaxKeys=2)
        self.assertEqual(len(keys), 2)
        self.assertTrue(r["IsTruncated"])
        self.assertIn("NextContinuationToken", r)

    def test_manual_continuation(self):
        seen, token = [], None
        while True:
            kw = {"MaxKeys": 2}
            if token:
                kw["ContinuationToken"] = token
            r, keys, _ = self.list(**kw)
            seen += keys
            if not r["IsTruncated"]:
                break
            token = r["NextContinuationToken"]
        self.assertEqual(seen, sorted(FILES))

    def test_paginator(self):
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, PaginationConfig={"PageSize": 2})
        keys = [o["Key"] for p in pages for o in p.get("Contents", [])]
        self.assertEqual(keys, sorted(FILES))

    def test_list_no_such_bucket(self):
        with self.assertRaises(ClientError) as cm:
            self.s3.list_objects_v2(Bucket="no-such-bucket-xyz")
        self.assertEqual(cm.exception.response["Error"]["Code"], "NoSuchBucket")
        self.assertEqual(cm.exception.response["ResponseMetadata"]["HTTPStatusCode"], 404)

    def test_get_object(self):
        r = self.s3.get_object(Bucket=BUCKET, Key="docs/f1.txt")
        self.assertEqual(r["Body"].read(), b"n1")
        self.assertEqual(r["ContentLength"], 2)

    def test_get_unicode_key(self):
        r = self.s3.get_object(Bucket=BUCKET, Key="한글/파일.txt")
        self.assertEqual(r["Body"].read(), FILES["한글/파일.txt"])

    def test_get_object_range(self):
        r = self.s3.get_object(Bucket=BUCKET, Key="docs/sub/z.txt", Range="bytes=1-")
        self.assertEqual(r["Body"].read(), b"zz")
        self.assertEqual(r["ContentRange"], "bytes 1-2/3")

    def test_get_object_no_such_key(self):
        with self.assertRaises(ClientError) as cm:
            self.s3.get_object(Bucket=BUCKET, Key="nope")
        self.assertEqual(cm.exception.response["Error"]["Code"], "NoSuchKey")

    def test_head_object(self):
        r = self.s3.head_object(Bucket=BUCKET, Key="root.txt")
        self.assertEqual(r["ContentLength"], 4)
        self.assertIn("LastModified", r)

    def test_head_object_missing(self):
        with self.assertRaises(ClientError) as cm:
            self.s3.head_object(Bucket=BUCKET, Key="nope")
        self.assertEqual(cm.exception.response["ResponseMetadata"]["HTTPStatusCode"], 404)

    def test_put_rejected(self):
        with self.assertRaises(ClientError) as cm:
            self.s3.put_object(Bucket=BUCKET, Key="new.txt", Body=b"x")
        self.assertEqual(cm.exception.response["ResponseMetadata"]["HTTPStatusCode"], 405)
        self.assertFalse(os.path.exists(os.path.join(self.bucket_dir, "new.txt")))

    def test_download_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "f1.txt")
            self.s3.download_file(BUCKET, "docs/f1.txt", dest)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), b"n1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
