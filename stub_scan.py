"""Test stand-in for NAPS2 (stdlib only).

  stub_scan.py scan  <out.jpg>                 -> writes a fake "scanned page" PNG
  stub_scan.py merge <out.pdf> <page> [<page>...] -> writes a PDF with one page per input
  stub_scan.py split <in.pdf> <outdir>            -> writes page001.png ... one per PDF page
"""
import itertools
import struct
import sys
import time
import zlib

_counter = itertools.count(1)
W, H = 210, 297


def png(path, n):
    # Light page with a coloured band whose hue depends on the page number.
    band = [(230, 80, 60), (60, 140, 230), (80, 190, 110), (240, 180, 50)][n % 4]
    rows = []
    for y in range(H):
        row = bytearray([0])
        for x in range(W):
            in_band = 30 < y < 60 or (x < 8) or (x > W - 9)
            row += bytes(band if in_band else (250, 250, 250))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def pdf(path, count):
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    for i in range(count):
        page_id, content_id = 3 + i * 2, 4 + i * 2
        kids.append(b"%d 0 R" % page_id)
        stream = b"BT /F1 24 Tf 30 100 Td (Stub page %d) Tj ET" % (i + 1)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (content_id, 3 + count * 2))
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), count)
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as f:
        f.write(out)


if __name__ == "__main__":
    action, out = sys.argv[1], sys.argv[2]
    if action == "scan":
        time.sleep(2)
        png(out, int(time.time()))
    elif action == "split":
        count = open(out, "rb").read().count(b"/Type /Page ")
        for i in range(count):
            png(f"{sys.argv[3]}/page{i + 1:03d}.png", i)
    else:
        pdf(out, len(sys.argv) - 3)
