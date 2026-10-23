"""Generate test PDFs with centered text for printer testing."""

import struct

def make_pdf(text, filename):
    """Create a minimal PDF with centered text on A4."""
    # A4 = 595 x 842 points
    # Center horizontally (~250 for short text), vertically (421 = middle)
    stream = f"BT /F1 28 Tf 220 421 Td ({text}) Tj ET"
    length = len(stream)

    pdf = f"""%PDF-1.4
1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj
2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj
3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>> endobj
4 0 obj <</Length {length}>>
stream
{stream}
endstream
endobj
5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj
xref
0 6
trailer <</Size 6 /Root 1 0 R>>
startxref
0
%%EOF
"""
    with open(filename, "w") as f:
        f.write(pdf)
    print(f"Created {filename}")


if __name__ == "__main__":
    make_pdf("PRINT TEST ONE", "/tmp/test1.pdf")
    make_pdf("PRINT TEST TWO", "/tmp/test2.pdf")
    make_pdf("PRINT TEST THREE", "/tmp/test3.pdf")
    print("Done. Now run:")
    print("  lp -d LBP2900 /tmp/test1.pdf")
    print("  sleep 30")
    print("  lp -d LBP2900 /tmp/test2.pdf")
    print("  sleep 30")
    print("  lp -d LBP2900 /tmp/test3.pdf")
