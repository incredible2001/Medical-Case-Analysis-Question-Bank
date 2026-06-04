"""将PDF拆分为每页一张PNG图片"""
import fitz  # PyMuPDF
import os
import time

PDF_PATH = "【番茄执医】病例分析330题真题速刷.pdf"
OUTPUT_DIR = "pages"
DPI = 200  # 200 DPI，兼顾清晰度和文件大小

def split_pdf():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc = fitz.open(PDF_PATH)
    total = len(doc)
    print(f"PDF共 {total} 页，开始拆分 (DPI={DPI})...")

    start_time = time.time()
    for i in range(total):
        out_path = os.path.join(OUTPUT_DIR, f"page_{i+1:03d}.png")
        if os.path.exists(out_path):
            print(f"  跳过 page_{i+1:03d}.png (已存在)")
            continue
        page = doc[i]
        mat = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=mat)
        pix.save(out_path)
        if (i + 1) % 10 == 0 or i == 0:
            elapsed = time.time() - start_time
            print(f"  已完成 {i+1}/{total} 页 ({elapsed:.1f}s)")

    doc.close()
    elapsed = time.time() - start_time
    print(f"\n拆分完成！共 {total} 页，耗时 {elapsed:.1f}s")
    print(f"图片保存在: {os.path.abspath(OUTPUT_DIR)}")

if __name__ == "__main__":
    split_pdf()
