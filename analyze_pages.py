"""
执业医师刷题 - 页面分析脚本 v2
OCR分栏 + API生成教学式解析（基于参考答案解读，不是重新做题）
支持断点续跑、多线程、日志记录
"""
import os
import sys
import re
import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv(override=True)

# ============ 配置 ============
API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "")
MODEL = os.getenv("ANTHROPIC_MODEL", "mimo-v2.5-pro")
PAGES_DIR = "pages"
SKIP_PAGES = 6  # 跳过封面/目录/说明
OUTPUT_DIR = "output"
RESULTS_FILE = os.path.join(OUTPUT_DIR, "results.json")
OCR_CACHE_FILE = os.path.join(OUTPUT_DIR, "ocr_cache.json")
LOG_FILE = os.path.join(OUTPUT_DIR, "analyze.log")
MAX_WORKERS = 3
MAX_RETRIES = 3
RETRY_DELAY = 5
TEST_MODE = False

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)
lock = threading.Lock()

# ============ OCR ============
_ocr_engine = None
_ocr_lock = threading.Lock()


def get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        with _ocr_lock:
            if _ocr_engine is None:
                logger.info("初始化 RapidOCR 引擎...")
                from rapidocr_onnxruntime import RapidOCR
                _ocr_engine = RapidOCR()
                logger.info("RapidOCR 初始化完成")
    return _ocr_engine


def clean_ocr_text(text: str) -> str:
    """清理OCR文本：合并被错误换行的句子"""
    lines = text.split("\n")
    merged = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 如果上一行不以句末标点结尾，且当前行不以序号/标题开头，则合并
        if merged and not re.search(r'[。！？：）\)】]$', merged[-1]) and not re.match(r'^[一二三四五六七八九十①②③④⑤⑥⑦⑧⑨⑩\d][、.）)]', line):
            # 添加空格分隔，防止OCR文本粘连导致关键数据丢失
            merged[-1] += " " + line
        else:
            merged.append(line)
    return "\n".join(merged)


def ocr_image(image_path: str) -> tuple:
    """OCR并分栏，返回 (题号, 左栏文本, 右栏文本)"""
    ocr = get_ocr()
    result, _ = ocr(image_path)
    if not result:
        return "", "", ""

    SPLIT_X = 1150
    left_lines = []
    right_lines = []
    question_num = ""

    for item in result:
        box, text, conf = item
        x_center = sum(p[0] for p in box) / 4
        y_top = min(p[1] for p in box)
        # 过滤水印
        if any(kw in text for kw in ["番茄", "bilibili", "Lynn", "2026执医", "技能笔记"]):
            continue

        # 提取题号（如 001. 或 002.）
        m = re.match(r'^(\d{3})[.．、]', text)
        if m and not question_num:
            question_num = m.group(1)

        if x_center < SPLIT_X:
            left_lines.append((y_top, text))
        else:
            right_lines.append((y_top, text))

    left_lines.sort(key=lambda x: x[0])
    right_lines.sort(key=lambda x: x[0])

    left_text = clean_ocr_text("\n".join(t for _, t in left_lines))
    right_text = clean_ocr_text("\n".join(t for _, t in right_lines))
    return question_num, left_text, right_text


# ============ 结果管理 ============
def load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def save_json(path: str, data: dict):
    with lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp, path)


def load_results() -> dict:
    data = load_json(RESULTS_FILE)
    if data:
        logger.info(f"加载已有结果: {len(data.get('pages', {}))} 页已完成")
    else:
        data = {"pages": {}, "meta": {"created": datetime.now().isoformat()}}
    return data


def save_results(data: dict):
    data["meta"]["updated"] = datetime.now().isoformat()
    data["meta"]["completed_count"] = len(data["pages"])
    save_json(RESULTS_FILE, data)


def load_ocr_cache() -> dict:
    return load_json(OCR_CACHE_FILE)


def save_ocr_cache(cache: dict):
    save_json(OCR_CACHE_FILE, cache)


# ============ API 分析 ============
PROMPT_TEMPLATE = """你是一位资深的执业医师考试辅导专家和培训讲师。
以下是一道执业医师病例分析题，已通过OCR从试卷中提取并分栏。

【左侧 - 病例描述/题干】
{left_text}

【右侧 - 参考答案与评分标准】
{right_text}

你的任务是：**根据右侧的参考答案，撰写一份教学式解析**。注意：不是让你重新做题，而是解读已给的答案，告诉考生"这个答案是怎么得出来的"、"怎么看出这个诊断的"。

请严格按照以下JSON格式输出（不要输出任何其他内容）：

{{
  "question_num": "{question_num}",
  "system": "所属临床系统分类（呼吸系统/循环系统/消化系统/泌尿系统/妇产科/血液系统/内分泌系统/神经系统/运动系统/风湿免疫/儿科/传染病/其他）",
  "disease": "诊断名称（从答案中提取，多个用分号分隔）",
  "scenario": "病例一句话概括（30字以内）",
  "question": "完整的病例描述文本（仅左侧题干部分）。合并OCR错误换行为连贯段落，但必须保留所有原始临床数据原文，包括心电图描述、检验数值、影像学结果等，一字不改",
  "answer": "右侧的完整参考答案（保留分值标注）",
  "explanation": {{
    "diagnosis_analysis": [
      {{
        "name": "诊断1名称",
        "score": "分值",
        "reasoning": "这个诊断是怎么看出来的？逐条列出关键线索（症状、体征、检查结果），用箭头或编号清晰展示推理链条"
      }},
      {{
        "name": "诊断2名称",
        "score": "分值",
        "reasoning": "推理过程..."
      }}
    ],
    "key_indicators": [
      {{
        "item": "指标或临床表现名称",
        "value": "本题数值/描述",
        "significance": "临床意义，为什么这个指标指向该诊断"
      }}
    ],
    "differential_diagnosis": [
      {{
        "name": "鉴别诊断名称",
        "score": "分值（如有）",
        "analysis": "为什么要鉴别这个病？和本题诊断的关键区别是什么？"
      }}
    ],
    "further_workup": "进一步检查的解读（基于答案内容，解释每项检查的目的）",
    "treatment_principles": "治疗原则的解读（基于答案内容，解释关键治疗措施的原理和注意事项）",
    "exam_tips": "做题技巧总结（2-3条，帮助考生快速识别同类题目）"
  }},
  "highlights": ["需要在题干中高亮标注的关键信息词句，用于复习时快速定位诊断线索"]
}}

注意：
1. explanation必须基于右侧参考答案来解读，不要自行诊断或给出不同答案
2. diagnosis_analysis 要像老师讲解一样：先给出诊断，再逐条列出"怎么看出来的"
3. key_indicators 列出题目中提到的所有关键检验指标和异常体征，附上正常参考范围
4. highlights 中的词句将在题干中高亮显示，选最能帮助记忆诊断的关键词。highlights 必须作为JSON的顶层字段，与question、answer、explanation同级
5. question字段必须完整保留所有原始临床数据，尤其是心电图（导联编号、ST段变化、波形描述）、检验结果（具体数值和单位）、影像学发现。绝对不允许用临床诊断术语替换原始检查描述（例如不允许把"V1~V6导联ST段弓背向上抬高"改写为"广泛前壁心肌梗死"）
6. 严格输出纯JSON，不要包含```json```标记或其他文字"""


def analyze_text(left_text: str, right_text: str, question_num: str, client) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        left_text=left_text,
        right_text=right_text,
        question_num=question_num or "未知"
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = ""
    for block in response.content:
        if type(block).__name__ == "TextBlock":
            raw = block.text.strip()
            break
    if not raw:
        for block in response.content:
            if type(block).__name__ == "ThinkingBlock" and hasattr(block, "thinking"):
                raw = block.thinking.strip()
                break
    if not raw:
        raise ValueError(f"API未返回内容, blocks={[type(b).__name__ for b in response.content]}")

    # 清理 markdown 包裹
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 修复字符串内的未转义换行符（逐字符处理）
        fixed_chars = []
        in_string = False
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == '"' and (i == 0 or raw[i-1] != '\\'):
                in_string = not in_string
                fixed_chars.append(ch)
            elif in_string and ch == '\n':
                fixed_chars.append('\\n')
            elif in_string and ch == '\r':
                pass  # skip
            else:
                fixed_chars.append(ch)
            i += 1
        fixed = ''.join(fixed_chars)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
        # 尝试修复截断JSON
        for end in range(len(fixed) - 1, 0, -1):
            if fixed[end] == "}":
                try:
                    return json.loads(fixed[:end + 1])
                except json.JSONDecodeError:
                    continue
        raise


def process_page(page_num: int, image_path: str, results: dict, ocr_cache: dict, client) -> bool:
    key = f"page_{page_num:03d}"

    with lock:
        if key in results["pages"] and results["pages"][key].get("status") == "ok":
            return True

    if not os.path.exists(image_path):
        logger.warning(f"[{key}] 图片不存在: {image_path}")
        return False

    # OCR
    with lock:
        cached = ocr_cache.get(key, {})
    if isinstance(cached, dict) and cached.get("left"):
        q_num = cached.get("q_num", "")
        left_text = cached["left"]
        right_text = cached.get("right", "")
        logger.info(f"[{key}] 使用OCR缓存 (左{len(left_text)}字 右{len(right_text)}字)")
    else:
        try:
            q_num, left_text, right_text = ocr_image(image_path)
            if not left_text.strip() and not right_text.strip():
                logger.warning(f"[{key}] OCR未识别到文字")
                return False
            with lock:
                ocr_cache[key] = {"q_num": q_num, "left": left_text, "right": right_text}
            save_ocr_cache(ocr_cache)
            logger.info(f"[{key}] OCR完成 题号={q_num} (左{len(left_text)}字 右{len(right_text)}字)")
        except Exception as e:
            logger.error(f"[{key}] OCR失败: {e}")
            return False

    # API
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[{key}] API分析 (尝试 {attempt}/{MAX_RETRIES})")
            result = analyze_text(left_text, right_text, q_num, client)
            result["page_num"] = page_num
            result["status"] = "ok"

            with lock:
                results["pages"][key] = result
            save_results(results)

            logger.info(f"[{key}] 完成 - 题号:{result.get('question_num','?')} 诊断:{result.get('disease','?')}")
            return True

        except json.JSONDecodeError as e:
            logger.warning(f"[{key}] JSON解析失败 (尝试 {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

        except Exception as e:
            logger.warning(f"[{key}] API失败 (尝试 {attempt}): {str(e)[:200]}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    with lock:
        results["pages"][key] = {"page_num": page_num, "status": "error", "error": "重试均失败"}
    save_results(results)
    logger.error(f"[{key}] 处理失败")
    return False


def main():
    if not API_KEY or API_KEY == "your-api-key-here":
        logger.error("请先在 .env 文件中填写 ANTHROPIC_API_KEY")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL)
    logger.info(f"API: base_url={BASE_URL}, model={MODEL}")

    results = load_results()
    ocr_cache = load_ocr_cache()

    all_pages = sorted(Path(PAGES_DIR).glob("page_*.png"))
    all_pages = all_pages[SKIP_PAGES:]
    logger.info(f"跳过前 {SKIP_PAGES} 页")

    if TEST_MODE:
        all_pages = all_pages[:3]
        logger.info(f"测试模式: 只处理前 {len(all_pages)} 页")

    pending = []
    for p in all_pages:
        num = int(p.stem.split("_")[1])
        key = f"page_{num:03d}"
        if results["pages"].get(key, {}).get("status") != "ok":
            pending.append((num, str(p)))

    total = len(all_pages)
    done = total - len(pending)
    logger.info(f"共 {total} 页，已完成 {done}，待处理 {len(pending)}")

    if not pending:
        logger.info("全部完成！")
        return

    get_ocr()
    start_time = time.time()
    success = fail = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_page, num, path, results, ocr_cache, client): num
            for num, path in pending
        }
        for future in as_completed(futures):
            num = futures[future]
            try:
                if future.result():
                    success += 1
                else:
                    fail += 1
            except Exception as e:
                logger.error(f"[page_{num:03d}] 异常: {e}")
                fail += 1
            completed = success + fail
            if completed % 10 == 0 or completed == len(pending):
                elapsed = time.time() - start_time
                logger.info(f"进度: {done+completed}/{total} (成功{success} 失败{fail} 耗时{elapsed:.0f}s)")

    elapsed = time.time() - start_time
    logger.info(f"\n完成！成功 {success}，失败 {fail}，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    if "--test" in sys.argv:
        TEST_MODE = True
        logger.info("=== 测试模式 ===")
    if "--workers" in sys.argv:
        idx = sys.argv.index("--workers")
        if idx + 1 < len(sys.argv):
            MAX_WORKERS = int(sys.argv[idx + 1])
    main()
