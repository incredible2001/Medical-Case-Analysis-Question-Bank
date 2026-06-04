"""将 results.json 注入 HTML 模板，生成刷题页面 v2
功能：页面范围分类、诊断高亮、章节大纲识别、版本信息
"""
import json
import os
import re
from datetime import datetime

RESULTS_FILE = "output/results.json"
TEMPLATE_FILE = "quiz_template.html"
OUTPUT_FILE = "output/quiz.html"
PLACEHOLDER = "__QUIZ_DATA_PLACEHOLDER__"
VERSION_PLACEHOLDER = "__VERSION__"
TIME_PLACEHOLDER = "__BUILD_TIME__"
OUTLINE_PLACEHOLDER = "__OUTLINE_DATA_PLACEHOLDER__"

SYSTEM_RANGES = [
    (7, 36, "呼吸系统"), (37, 60, "循环系统"), (61, 137, "消化系统"),
    (138, 163, "泌尿系统"), (164, 194, "妇产科"), (195, 208, "血液系统"),
    (209, 222, "内分泌系统"), (223, 236, "神经系统"), (237, 261, "运动系统"),
    (262, 270, "风湿免疫"), (271, 300, "儿科"), (301, 317, "传染病"),
    (318, 348, "其他"),
]


def get_system_by_page(page_num):
    for start, end, name in SYSTEM_RANGES:
        if start <= page_num <= end:
            return name
    return "其他"


def is_outline_page(q, ocr_cache):
    """判断是否为章节大纲/技巧页（基于OCR缓存）"""
    key = f"page_{q.get('page_num',0):03d}"
    cached = ocr_cache.get(key, {})
    left = ""
    if isinstance(cached, dict):
        left = cached.get("left", "")
    elif isinstance(cached, str):
        left = cached
    keywords = ["大纲", "主诊断只可能是", "常见副诊断", "答题技巧", "疾病\n"]
    return any(kw in left for kw in keywords)


def clean_question_text(text):
    if not text:
        return ""
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def main():
    if not os.path.exists(RESULTS_FILE):
        print(f"错误: {RESULTS_FILE} 不存在")
        return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    OCR_CACHE_FILE = "output/ocr_cache.json"
    ocr_cache = {}
    if os.path.exists(OCR_CACHE_FILE):
        with open(OCR_CACHE_FILE, "r", encoding="utf-8") as f:
            ocr_cache = json.load(f)

    pages = data.get("pages", {})
    outlines = {}
    questions = {}

    for key, q in pages.items():
        if q.get("status") != "ok":
            continue
        page_num = q.get("page_num", 0)
        # 使用results.json中已有的系统分类（已按题号重新分配）
        if not q.get("system"):
            q["system"] = get_system_by_page(page_num)
        if q.get("question"):
            q["question"] = clean_question_text(q["question"])

        if is_outline_page(q, ocr_cache):
            q["type"] = "outline"
            # 存储OCR原始文本用于显示大纲内容
            key_ocr = f"page_{page_num:03d}"
            cached = ocr_cache.get(key_ocr, {})
            q["ocr_left"] = cached.get("left", "") if isinstance(cached, dict) else ""
            q["ocr_right"] = cached.get("right", "") if isinstance(cached, dict) else ""
            outlines[key] = q
        else:
            q["type"] = "question"
            questions[key] = q

    print(f"题目: {len(questions)} 道, 大纲: {len(outlines)} 页")

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    html = html.replace(PLACEHOLDER, json.dumps(questions, ensure_ascii=False))
    html = html.replace(OUTLINE_PLACEHOLDER, json.dumps(outlines, ensure_ascii=False))
    html = html.replace(VERSION_PLACEHOLDER, "v1.0")
    html = html.replace(TIME_PLACEHOLDER, datetime.now().strftime("%Y-%m-%d %H:%M"))

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"已生成 {OUTPUT_FILE} ({size_kb:.0f} KB)")

    # 统计
    sys_counts = {}
    for q in questions.values():
        s = q.get("system", "其他")
        sys_counts[s] = sys_counts.get(s, 0) + 1
    print("\n各系统题目数:")
    for s in ["呼吸系统","循环系统","消化系统","泌尿系统","妇产科","血液系统","内分泌系统","神经系统","运动系统","风湿免疫","儿科","传染病","其他"]:
        if s in sys_counts:
            print(f"  {s}: {sys_counts[s]}题")


if __name__ == "__main__":
    main()
