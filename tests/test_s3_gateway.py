#!/usr/bin/env python3
"""Black-box tests for the read-only S3 gateway (S3GetObjectServer).

Stdlib only. Fixtures are written straight into the server's data root, so the
tests need filesystem access to it:

    S3_ENDPOINT=http://localhost:9020 S3_DATA_DIR=../data python3 -m unittest -v test_s3_gateway

Defaults: endpoint http://localhost:9020, data dir ../data (relative to this file).
A temporary bucket directory is created and removed afterwards.
"""
import http.client
import os
import shutil
import unittest
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9020")
DATA_DIR = os.path.abspath(os.environ.get(
    "S3_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data")))
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

BUCKET = "test-" + uuid.uuid4().hex[:12]
BIG = bytes(range(256)) * 1024 * 5  # 1.25 MiB, spans several 64 KiB buffers
FILES = {
    "hello.txt": b"hello world",
    "empty.bin": b"",
    "big.bin": BIG,
    "dir/a.txt": b"a",
    "dir/b.txt": b"bb",
    "dir/sub/c.txt": b"ccc",
    "other/d.txt": b"dddd",
    "space name.txt": b"with space",
    "plus+sign.txt": b"plus",
    "&<>'\".txt": b"xml chars",
    "한글/파일.txt": "유니코드".encode(),
}


def request(method, path, headers=None):
    """Send a raw request (no client-side path normalisation)."""
    u = urllib.parse.urlparse(ENDPOINT)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


def quote(key):
    return urllib.parse.quote(key, safe="/")


def obj(key):
    return "/%s/%s" % (BUCKET, quote(key))


def error_code(body):
    return ET.fromstring(body).findtext("Code")


def list_v2(**params):
    q = urllib.parse.urlencode({"list-type": "2", **params}, quote_via=urllib.parse.quote)
    status, headers, body = request("GET", "/%s?%s" % (BUCKET, q))
    assert status == 200, (status, body)
    root = ET.fromstring(body)
    keys = [e.findtext("s3:Key", namespaces=NS) for e in root.findall("s3:Contents", NS)]
    prefixes = [e.findtext("s3:Prefix", namespaces=NS) for e in root.findall("s3:CommonPrefixes", NS)]
    return root, keys, prefixes


class S3GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            request("GET", "/")
        except OSError as e:
            raise unittest.SkipTest("server not reachable at %s: %s" % (ENDPOINT, e))
        if not os.path.isdir(DATA_DIR):
            raise unittest.SkipTest("data dir not found: %s (set S3_DATA_DIR)" % DATA_DIR)
        cls.bucket_dir = os.path.join(DATA_DIR, BUCKET)
        for key, content in FILES.items():
            path = os.path.join(cls.bucket_dir, *key.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "bucket_dir", "/nonexistent-skip"), ignore_errors=True)

    # ---- GetObject -------------------------------------------------------
    def test_get_object(self):
        status, h, body = request("GET", obj("hello.txt"))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello world")
        self.assertEqual(h["content-length"], "11")
        self.assertEqual(h["accept-ranges"], "bytes")
        self.assertIn("last-modified", h)
        self.assertTrue(h["last-modified"].endswith("GMT"))

    def test_get_large_object(self):
        status, h, body = request("GET", obj("big.bin"))
        self.assertEqual(status, 200)
        self.assertEqual(body, BIG)

    def test_get_empty_object(self):
        status, h, body = request("GET", obj("empty.bin"))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(h["content-length"], "0")

    def test_get_nested_key(self):
        status, _, body = request("GET", obj("dir/sub/c.txt"))
        self.assertEqual((status, body), (200, b"ccc"))

    def test_get_special_char_keys(self):
        for key in ("space name.txt", "plus+sign.txt", "&<>'\".txt", "한글/파일.txt"):
            with self.subTest(key=key):
                status, _, body = request("GET", obj(key))
                self.assertEqual(status, 200)
                self.assertEqual(body, FILES[key])

    def test_get_missing_key(self):
        status, _, body = request("GET", obj("nope.txt"))
        self.assertEqual(status, 404)
        self.assertEqual(error_code(body), "NoSuchKey")

    def test_get_directory_as_key(self):
        status, _, body = request("GET", obj("dir"))
        self.assertEqual(status, 404)
        self.assertEqual(error_code(body), "NoSuchKey")

    def test_get_missing_bucket(self):
        status, _, body = request("GET", "/no-such-bucket-xyz/hello.txt")
        self.assertEqual(status, 404)
        self.assertEqual(error_code(body), "NoSuchBucket")

    def test_get_trailing_slash_key(self):
        status, _, body = request("GET", "/%s/" % BUCKET)
        self.assertEqual(status, 400)
        self.assertEqual(error_code(body), "InvalidURI")

    # ---- HeadObject ------------------------------------------------------
    def test_head_object(self):
        status, h, body = request("HEAD", obj("hello.txt"))
        self.assertEqual(status, 200)
        self.assertEqual(h["content-length"], "11")
        self.assertEqual(h["accept-ranges"], "bytes")
        self.assertIn("last-modified", h)
        self.assertEqual(body, b"")

    def test_head_matches_get_headers(self):
        _, hh, _ = request("HEAD", obj("big.bin"))
        _, gh, _ = request("GET", obj("big.bin"))
        for name in ("content-length", "content-type", "last-modified"):
            self.assertEqual(hh[name], gh[name], name)

    def test_head_missing_key(self):
        status, _, body = request("HEAD", obj("nope.txt"))
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_head_missing_bucket(self):
        status, _, _ = request("HEAD", "/no-such-bucket-xyz/hello.txt")
        self.assertEqual(status, 404)

    def test_head_empty_object(self):
        status, h, _ = request("HEAD", obj("empty.bin"))
        self.assertEqual(status, 200)
        self.assertEqual(h["content-length"], "0")

    # ---- Range -----------------------------------------------------------
    def rng(self, spec, key="hello.txt"):
        return request("GET", obj(key), {"Range": spec})

    def test_range_start_end(self):
        status, h, body = self.rng("bytes=0-4")
        self.assertEqual(status, 206)
        self.assertEqual(body, b"hello")
        self.assertEqual(h["content-range"], "bytes 0-4/11")
        self.assertEqual(h["content-length"], "5")

    def test_range_open_ended(self):
        status, h, body = self.rng("bytes=6-")
        self.assertEqual((status, body), (206, b"world"))
        self.assertEqual(h["content-range"], "bytes 6-10/11")

    def test_range_suffix(self):
        status, h, body = self.rng("bytes=-5")
        self.assertEqual((status, body), (206, b"world"))
        self.assertEqual(h["content-range"], "bytes 6-10/11")

    def test_range_suffix_larger_than_object(self):
        status, h, body = self.rng("bytes=-100")
        self.assertEqual((status, body), (206, b"hello world"))
        self.assertEqual(h["content-range"], "bytes 0-10/11")

    def test_range_end_clamped(self):
        status, h, body = self.rng("bytes=6-1000")
        self.assertEqual((status, body), (206, b"world"))
        self.assertEqual(h["content-range"], "bytes 6-10/11")

    def test_range_single_byte(self):
        status, _, body = self.rng("bytes=0-0")
        self.assertEqual((status, body), (206, b"h"))

    def test_range_across_buffer_boundary(self):
        start, end = 65530, 65545
        status, _, body = self.rng("bytes=%d-%d" % (start, end), "big.bin")
        self.assertEqual(status, 206)
        self.assertEqual(body, BIG[start:end + 1])

    def test_range_large_tail(self):
        status, _, body = self.rng("bytes=1000000-", "big.bin")
        self.assertEqual(status, 206)
        self.assertEqual(body, BIG[1000000:])

    def test_range_unsatisfiable(self):
        status, h, body = self.rng("bytes=11-20")
        self.assertEqual(status, 416)
        self.assertEqual(h["content-range"], "bytes */11")
        self.assertEqual(error_code(body), "InvalidRange")

    def test_range_invalid_syntax(self):
        for spec in ("bytes=abc", "items=0-1", "bytes=5-2", "bytes=0-1,3-4", "bytes=-0", "bytes=-"):
            with self.subTest(spec=spec):
                status, _, _ = self.rng(spec)
                self.assertEqual(status, 416)

    def test_range_on_empty_object(self):
        status, _, _ = self.rng("bytes=0-0", "empty.bin")
        self.assertEqual(status, 416)

    def test_head_ignores_range(self):
        status, h, _ = request("HEAD", obj("hello.txt"), {"Range": "bytes=0-4"})
        self.assertEqual(status, 200)
        self.assertEqual(h["content-length"], "11")

    # ---- Unsupported methods --------------------------------------------
    def test_write_methods_rejected(self):
        for method in ("PUT", "POST", "DELETE"):
            with self.subTest(method=method):
                status, h, body = request(method, obj("hello.txt"))
                self.assertEqual(status, 405)
                self.assertEqual(h.get("allow"), "GET, HEAD")
                self.assertEqual(error_code(body), "MethodNotAllowed")
        # object must be untouched
        self.assertEqual(request("GET", obj("hello.txt"))[2], b"hello world")

    # ---- Path safety -----------------------------------------------------
    def test_path_traversal_key(self):
        secret = os.path.join(DATA_DIR, "secret-%s.txt" % uuid.uuid4().hex[:8])
        with open(secret, "w") as f:
            f.write("secret")
        try:
            name = os.path.basename(secret)
            for path in ("/%s/../%s" % (BUCKET, name),
                         "/%s/%%2e%%2e/%s" % (BUCKET, name),
                         "/%s/dir/../../%s" % (BUCKET, name),
                         "/%s/%%2e%%2e%%2f%s" % (BUCKET, name)):
                with self.subTest(path=path):
                    status, _, body = request("GET", path)
                    self.assertIn(status, (400, 403, 404))
                    self.assertNotIn(b"secret", body)
        finally:
            os.remove(secret)

    def test_invalid_bucket_names(self):
        for path in ("/", "/..", "/%2e%2e/x", "/./x", "/%5cwin/x"):
            with self.subTest(path=path):
                status, _, body = request("GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(error_code(body), "InvalidBucketName")

    def test_backslash_in_key(self):
        status, _, body = request("GET", "/%s/dir%%5ca.txt" % BUCKET)
        self.assertEqual(status, 403)
        self.assertEqual(error_code(body), "AccessDenied")

    def test_bad_percent_encoding(self):
        # rejected by the JDK HttpServer itself (HTML body), so only check status
        status, _, _ = request("GET", "/%s/%%zz" % BUCKET)
        self.assertEqual(status, 400)

    def test_symlink_escape_blocked(self):
        outside = os.path.join(DATA_DIR, "outside-%s.txt" % uuid.uuid4().hex[:8])
        link = os.path.join(self.bucket_dir, "link.txt")
        with open(outside, "w") as f:
            f.write("outside")
        try:
            os.symlink(outside, link)
        except OSError:
            os.remove(outside)
            self.skipTest("symlinks not supported")
        try:
            status, _, body = request("GET", obj("link.txt"))
            self.assertEqual(status, 404)
            self.assertNotIn(b"outside", body)
            _, keys, _ = list_v2()
            self.assertNotIn("link.txt", keys)
        finally:
            os.remove(link)
            os.remove(outside)

    # ---- ListObjectsV2 ---------------------------------------------------
    def test_list_all(self):
        root, keys, prefixes = list_v2()
        self.assertEqual(sorted(keys), sorted(FILES))
        self.assertEqual(keys, sorted(keys), "keys must be sorted")
        self.assertEqual(prefixes, [])
        self.assertEqual(root.findtext("s3:Name", namespaces=NS), BUCKET)
        self.assertEqual(root.findtext("s3:IsTruncated", namespaces=NS), "false")
        self.assertEqual(root.findtext("s3:KeyCount", namespaces=NS), str(len(FILES)))
        self.assertEqual(root.findtext("s3:MaxKeys", namespaces=NS), "1000")

    def test_list_contents_fields(self):
        root, _, _ = list_v2(prefix="dir/sub/")
        c = root.find("s3:Contents", NS)
        self.assertEqual(c.findtext("s3:Key", namespaces=NS), "dir/sub/c.txt")
        self.assertEqual(c.findtext("s3:Size", namespaces=NS), "3")
        self.assertEqual(c.findtext("s3:StorageClass", namespaces=NS), "STANDARD")
        self.assertRegex(c.findtext("s3:LastModified", namespaces=NS),
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_list_trailing_slash_bucket(self):
        status, _, _ = request("GET", "/%s/?list-type=2" % BUCKET)
        self.assertEqual(status, 200)

    def test_list_prefix(self):
        _, keys, _ = list_v2(prefix="dir/")
        self.assertEqual(keys, ["dir/a.txt", "dir/b.txt", "dir/sub/c.txt"])

    def test_list_prefix_no_match(self):
        root, keys, _ = list_v2(prefix="zzz")
        self.assertEqual(keys, [])
        self.assertEqual(root.findtext("s3:KeyCount", namespaces=NS), "0")

    def test_list_delimiter_root(self):
        _, keys, prefixes = list_v2(delimiter="/")
        self.assertEqual(sorted(prefixes), ["dir/", "other/", "한글/"])
        self.assertEqual(sorted(keys), sorted(k for k in FILES if "/" not in k))

    def test_list_delimiter_with_prefix(self):
        root, keys, prefixes = list_v2(prefix="dir/", delimiter="/")
        self.assertEqual(keys, ["dir/a.txt", "dir/b.txt"])
        self.assertEqual(prefixes, ["dir/sub/"])
        self.assertEqual(root.findtext("s3:Delimiter", namespaces=NS), "/")
        self.assertEqual(root.findtext("s3:Prefix", namespaces=NS), "dir/")

    def test_list_xml_escaping_roundtrip(self):
        _, keys, _ = list_v2(prefix="&")
        self.assertEqual(keys, ["&<>'\".txt"])

    def test_list_max_keys(self):
        root, keys, _ = list_v2(**{"max-keys": "3"})
        self.assertEqual(len(keys), 3)
        self.assertEqual(root.findtext("s3:IsTruncated", namespaces=NS), "true")
        self.assertEqual(root.findtext("s3:MaxKeys", namespaces=NS), "3")
        self.assertIsNotNone(root.findtext("s3:NextContinuationToken", namespaces=NS))

    def test_list_max_keys_zero(self):
        root, keys, _ = list_v2(**{"max-keys": "0"})
        self.assertEqual(keys, [])
        self.assertEqual(root.findtext("s3:KeyCount", namespaces=NS), "0")

    def test_list_max_keys_capped(self):
        root, _, _ = list_v2(**{"max-keys": "99999"})
        self.assertEqual(root.findtext("s3:MaxKeys", namespaces=NS), "1000")

    def test_list_pagination_covers_everything_once(self):
        seen, token = [], None
        for _ in range(len(FILES) + 2):
            params = {"max-keys": "4"}
            if token:
                params["continuation-token"] = token
            root, keys, _ = list_v2(**params)
            seen += keys
            if root.findtext("s3:IsTruncated", namespaces=NS) == "false":
                break
            token = root.findtext("s3:NextContinuationToken", namespaces=NS)
            self.assertTrue(token)
        else:
            self.fail("pagination did not terminate")
        self.assertEqual(seen, sorted(FILES))

    def test_list_pagination_echoes_token(self):
        root, _, _ = list_v2(**{"max-keys": "2"})
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)
        root2, _, _ = list_v2(**{"max-keys": "2", "continuation-token": token})
        self.assertEqual(root2.findtext("s3:ContinuationToken", namespaces=NS), token)

    def test_list_pagination_with_delimiter(self):
        root, keys, prefixes = list_v2(delimiter="/", **{"max-keys": "2"})
        self.assertEqual(len(keys) + len(prefixes), 2)
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)
        _, keys2, prefixes2 = list_v2(delimiter="/", **{"max-keys": "100", "continuation-token": token})
        total = set(keys + keys2) | set(prefixes + prefixes2)
        self.assertEqual(len(total), len(keys + keys2 + prefixes + prefixes2), "no duplicates")

    def test_list_invalid_max_keys(self):
        for v in ("abc", "-1"):
            with self.subTest(v=v):
                status, _, body = request("GET", "/%s?list-type=2&max-keys=%s" % (BUCKET, v))
                self.assertEqual(status, 400)
                self.assertEqual(error_code(body), "InvalidArgument")

    def test_list_invalid_continuation_token(self):
        for token in ("!!!notbase64", "bm90LWEtbnVtYmVy", "LTE"):  # garbage, "not-a-number", "-1"
            with self.subTest(token=token):
                status, _, body = request("GET", "/%s?list-type=2&continuation-token=%s" % (BUCKET, token))
                self.assertEqual(status, 400)
                self.assertEqual(error_code(body), "InvalidArgument")

    def test_list_token_beyond_end(self):
        import base64
        token = base64.urlsafe_b64encode(b"9999").decode().rstrip("=")
        status, _, body = request("GET", "/%s?list-type=2&continuation-token=%s" % (BUCKET, token))
        self.assertEqual(status, 400)

    def test_list_without_list_type_not_implemented(self):
        status, _, body = request("GET", "/%s" % BUCKET)
        self.assertEqual(status, 501)
        self.assertEqual(error_code(body), "NotImplemented")

    def test_list_type_1_not_implemented(self):
        status, _, body = request("GET", "/%s?list-type=1" % BUCKET)
        self.assertEqual(status, 501)

    def test_list_missing_bucket(self):
        status, _, body = request("GET", "/no-such-bucket-xyz?list-type=2")
        self.assertEqual(status, 404)
        self.assertEqual(error_code(body), "NoSuchBucket")

    def test_list_head_not_allowed(self):
        status, _, _ = request("HEAD", "/%s?list-type=2" % BUCKET)
        self.assertEqual(status, 405)

    def test_list_content_type(self):
        _, h, _ = request("GET", "/%s?list-type=2" % BUCKET)
        self.assertTrue(h["content-type"].startswith("application/xml"))

    # ---- Concurrency -----------------------------------------------------
    def test_concurrent_reads(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(lambda _: request("GET", obj("big.bin"))[2], range(32)))
        self.assertTrue(all(r == BIG for r in results))


if __name__ == "__main__":
    unittest.main(verbosity=2)
