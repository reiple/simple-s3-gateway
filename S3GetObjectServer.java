import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.Executors;
import java.util.stream.Stream;

/** Java 8 / path-style S3 read-only gateway backed by local files. */
public class S3GetObjectServer {
    private static final DateTimeFormatter HTTP_DATE =
            DateTimeFormatter.RFC_1123_DATE_TIME.withZone(ZoneOffset.UTC);
    private static final DateTimeFormatter S3_DATE =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC);
    private static final ThreadLocal<Integer> STATUS = new ThreadLocal<>();
    private static final ThreadLocal<Long> BYTES = new ThreadLocal<>();

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 9000;
        Path root = Paths.get(args.length > 1 ? args[1] : "/data/s3").toRealPath();
        if (!Files.isDirectory(root)) throw new IllegalArgumentException("Invalid root: " + root);
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", ex -> handle(ex, root));
        server.setExecutor(Executors.newFixedThreadPool(16));
        server.start();
        System.out.println("S3 read-only server listening on " + port + " root=" + root);
    }

    private static void handle(HttpExchange ex, Path root) throws IOException {
        long started = System.nanoTime();
        STATUS.set(500);
        BYTES.set(0L);
        try {
            String method = ex.getRequestMethod();
            if (!method.equals("GET") && !method.equals("HEAD")) {
                ex.getResponseHeaders().set("Allow", "GET, HEAD");
                error(ex, 405, "MethodNotAllowed", "Only GET and HEAD are supported");
                return;
            }
            String path;
            try {
                path = decode(ex.getRequestURI().getRawPath());
            } catch (IllegalArgumentException e) {
                error(ex, 400, "InvalidURI", "Invalid path encoding");
                return;
            }
            if (!path.startsWith("/")) { error(ex, 400, "InvalidURI", "Invalid path"); return; }
            String relative = path.substring(1);
            int slash = relative.indexOf('/');
            String bucket = slash < 0 ? relative : relative.substring(0, slash);
            if (bucket.isEmpty() || bucket.equals(".") || bucket.equals("..") || bucket.contains("\\")) {
                error(ex, 400, "InvalidBucketName", "Invalid bucket"); return;
            }
            Path bucketDir = root.resolve(bucket).normalize();
            if (!bucketDir.startsWith(root) || !Files.isDirectory(bucketDir) ||
                    !bucketDir.toRealPath().startsWith(root)) {
                error(ex, 404, "NoSuchBucket", "Bucket not found"); return;
            }
            if (slash < 0 || (slash == relative.length() - 1 && query(ex, "list-type") != null)) {
                if (!method.equals("GET")) { error(ex, 405, "MethodNotAllowed", "List requires GET"); return; }
                if (!"2".equals(query(ex, "list-type"))) {
                    error(ex, 501, "NotImplemented", "Only ListObjectsV2 is implemented"); return;
                }
                listV2(ex, bucket, bucketDir);
                return;
            }
            if (slash < 0 || slash == relative.length() - 1) {
                error(ex, 400, "InvalidURI", "Expected /bucket/key"); return;
            }
            String key = relative.substring(slash + 1);
            if (key.indexOf('\\') >= 0) { error(ex, 403, "AccessDenied", "Invalid key"); return; }
            Path file = bucketDir.resolve(key).normalize();
            if (!file.startsWith(bucketDir) || !Files.isRegularFile(file) ||
                    !file.toRealPath().startsWith(bucketDir.toRealPath())) {
                error(ex, 404, "NoSuchKey", "Object not found"); return;
            }
            object(ex, file);
        } catch (Exception e) {
            System.err.println("Request error: " + e);
            if (STATUS.get() == 500) {
                try { error(ex, 500, "InternalError", "Request failed"); }
                catch (Exception ignored) { }
            }
        } finally {
            String uri = ex.getRequestURI().toString().replace("\r", "%0D").replace("\n", "%0A");
            System.out.printf("%s client=%s method=%s uri=\"%s\" status=%d bytes=%d duration_ms=%d%n",
                    Instant.now(), ex.getRemoteAddress().getAddress().getHostAddress(),
                    ex.getRequestMethod(), uri, STATUS.get(), BYTES.get(),
                    (System.nanoTime() - started) / 1000000);
            STATUS.remove(); BYTES.remove(); ex.close();
        }
    }

    private static void object(HttpExchange ex, Path file) throws IOException {
        long size = Files.size(file);
        Headers h = ex.getResponseHeaders();
        String type = Files.probeContentType(file);
        h.set("Content-Type", type != null ? type : "application/octet-stream");
        h.set("Accept-Ranges", "bytes");
        h.set("Last-Modified", HTTP_DATE.format(Files.getLastModifiedTime(file).toInstant()));
        long from = 0, to = size - 1;
        int status = 200;
        String rangeHeader = ex.getRequestHeaders().getFirst("Range");
        if (ex.getRequestMethod().equals("GET") && rangeHeader != null) {
            long[] range = range(rangeHeader, size);
            if (range == null) {
                h.set("Content-Range", "bytes */" + size);
                error(ex, 416, "InvalidRange", "Requested range not satisfiable");
                return;
            }
            from = range[0]; to = range[1]; status = 206;
            h.set("Content-Range", "bytes " + from + "-" + to + "/" + size);
        }
        long length = status == 206 ? to - from + 1 : size;
        STATUS.set(status);
        if (ex.getRequestMethod().equals("HEAD") || length == 0) {
            h.set("Content-Length", Long.toString(length));
            ex.sendResponseHeaders(status, -1);
            return;
        }
        ex.sendResponseHeaders(status, length);
        try (InputStream in = Files.newInputStream(file); OutputStream out = ex.getResponseBody()) {
            long skip = from;
            while (skip > 0) {
                long n = in.skip(skip);
                if (n <= 0) { if (in.read() == -1) throw new EOFException(); n = 1; }
                skip -= n;
            }
            byte[] buffer = new byte[65536];
            long remaining = length;
            while (remaining > 0) {
                int n = in.read(buffer, 0, (int)Math.min(buffer.length, remaining));
                if (n < 0) throw new EOFException();
                out.write(buffer, 0, n);
                remaining -= n;
                BYTES.set(length - remaining);
            }
        }
    }

    private static void listV2(HttpExchange ex, String bucket, Path dir) throws IOException {
        String prefix = defaultString(query(ex, "prefix"));
        String delimiter = defaultString(query(ex, "delimiter"));
        String maxValue = query(ex, "max-keys");
        int max = 1000;
        try {
            if (maxValue != null) max = Integer.parseInt(maxValue);
            if (max < 0) throw new NumberFormatException();
            max = Math.min(max, 1000);
        } catch (NumberFormatException e) { error(ex, 400, "InvalidArgument", "Invalid max-keys"); return; }
        int offset = 0;
        String token = query(ex, "continuation-token");
        try {
            if (token != null) offset = Integer.parseInt(new String(
                    Base64.getUrlDecoder().decode(token), StandardCharsets.UTF_8));
            if (offset < 0) throw new NumberFormatException();
        } catch (Exception e) { error(ex, 400, "InvalidArgument", "Invalid continuation token"); return; }
        final SortedMap<String, Entry> entries = new TreeMap<>();
        try (Stream<Path> paths = Files.walk(dir)) {
            paths.filter(Files::isRegularFile).forEach(file -> {
                try {
                    if (!file.toRealPath().startsWith(dir.toRealPath())) return;
                    String key = dir.relativize(file).toString().replace(File.separatorChar, '/');
                    if (!key.startsWith(prefix)) return;
                    if (!delimiter.isEmpty()) {
                        int pos = key.indexOf(delimiter, prefix.length());
                        if (pos >= 0) {
                            String common = key.substring(0, pos + delimiter.length());
                            entries.put(common, new Entry(common, null));
                            return;
                        }
                    }
                    entries.put(key, new Entry(key, file));
                } catch (IOException ignored) { }
            });
        }
        if (offset > entries.size()) { error(ex, 400, "InvalidArgument", "Expired continuation token"); return; }
        List<Entry> all = new ArrayList<>(entries.values());
        int end = (int)Math.min((long)all.size(), (long)offset + max);
        boolean truncated = end < all.size();
        StringBuilder xml = new StringBuilder("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
        xml.append("<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">");
        tag(xml, "Name", bucket); tag(xml, "Prefix", prefix);
        tag(xml, "MaxKeys", String.valueOf(max));
        tag(xml, "KeyCount", String.valueOf(end - offset));
        tag(xml, "IsTruncated", String.valueOf(truncated));
        if (!delimiter.isEmpty()) tag(xml, "Delimiter", delimiter);
        if (token != null) tag(xml, "ContinuationToken", token);
        if (truncated && end > offset) tag(xml, "NextContinuationToken",
                Base64.getUrlEncoder().withoutPadding().encodeToString(
                        String.valueOf(end).getBytes(StandardCharsets.UTF_8)));
        for (int i = offset; i < end; i++) {
            Entry entry = all.get(i);
            if (entry.file == null) {
                xml.append("<CommonPrefixes>"); tag(xml, "Prefix", entry.key);
                xml.append("</CommonPrefixes>");
            } else {
                xml.append("<Contents>"); tag(xml, "Key", entry.key);
                tag(xml, "LastModified", S3_DATE.format(Files.getLastModifiedTime(entry.file).toInstant()));
                tag(xml, "Size", String.valueOf(Files.size(entry.file)));
                tag(xml, "StorageClass", "STANDARD");
                xml.append("</Contents>");
            }
        }
        xml.append("</ListBucketResult>");
        reply(ex, 200, xml.toString());
    }

    private static class Entry {
        final String key; final Path file;
        Entry(String key, Path file) { this.key = key; this.file = file; }
    }
    private static String defaultString(String s) { return s == null ? "" : s; }
    private static String query(HttpExchange ex, String name) {
        String raw = ex.getRequestURI().getRawQuery();
        if (raw == null) return null;
        for (String pair : raw.split("&")) {
            String[] kv = pair.split("=", 2);
            if (decode(kv[0]).equals(name)) return kv.length > 1 ? decode(kv[1]) : "";
        }
        return null;
    }
    private static String decode(String raw) {
        try { return URLDecoder.decode(raw.replace("+", "%2B"), "UTF-8"); }
        catch (UnsupportedEncodingException e) { throw new IllegalStateException(e); }
    }
    private static void tag(StringBuilder sb, String key, String value) {
        sb.append('<').append(key).append('>').append(xmlEscape(value))
          .append("</").append(key).append('>');
    }
    private static String xmlEscape(String s) {
        return s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&apos;");
    }
    private static long[] range(String header, long size) {
        if (!header.startsWith("bytes=") || size == 0) return null;
        String value = header.substring(6).trim();
        if (value.contains(",")) return null;
        int dash = value.indexOf('-');
        if (dash < 0) return null;
        try {
            String a = value.substring(0, dash).trim();
            String b = value.substring(dash + 1).trim();
            if (a.isEmpty()) {
                long suffix = Long.parseLong(b);
                if (suffix <= 0) return null;
                return new long[]{size - Math.min(size, suffix), size - 1};
            }
            long start = Long.parseLong(a);
            long end = b.isEmpty() ? size - 1 : Long.parseLong(b);
            if (start < 0 || start >= size || end < start) return null;
            return new long[]{start, Math.min(end, size - 1)};
        } catch (NumberFormatException e) { return null; }
    }
    private static void error(HttpExchange ex, int status, String code, String message) throws IOException {
        StringBuilder xml = new StringBuilder("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Error>");
        tag(xml, "Code", code); tag(xml, "Message", message); xml.append("</Error>");
        reply(ex, status, xml.toString());
    }
    private static void reply(HttpExchange ex, int status, String xml) throws IOException {
        byte[] bytes = xml.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/xml; charset=utf-8");
        STATUS.set(status);
        if (ex.getRequestMethod().equals("HEAD")) {
            ex.getResponseHeaders().set("Content-Length", String.valueOf(bytes.length));
            ex.sendResponseHeaders(status, -1);
        } else {
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = ex.getResponseBody()) { out.write(bytes); BYTES.set((long)bytes.length); }
        }
    }
}

