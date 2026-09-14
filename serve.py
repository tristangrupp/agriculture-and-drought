"""Static server with HTTP range support, which DuckDB needs to read parquet by row group.

Python's stock SimpleHTTPRequestHandler ignores Range headers and returns the whole file
with a 200, which silently turns every "prune to one row group" query into a full-file
download. This adds 206 handling and permissive CORS.

    python serve.py [port]
"""
import os, re, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


class RangeHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        # no-store, not no-cache: "no-cache" still lets the browser keep a copy and
        # revalidate, which served a stale (and briefly invalid) data file after the
        # pipeline had already rewritten it. During development the data changes under
        # the page constantly, so never store it.
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_GET()
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if not m:
            return super().do_GET()
        s, e = m.group(1), m.group(2)
        start = int(s) if s else max(0, size - int(e))
        end = int(e) if e and s else size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    os.chdir(ROOT)
    print("serving %s on http://127.0.0.1:%d  (range requests enabled)" % (ROOT, port))
    ThreadingHTTPServer(("127.0.0.1", port), RangeHandler).serve_forever()
