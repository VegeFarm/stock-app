import re
import difflib
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import cv2
import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image
import pytesseract


# -----------------------------
# 유틸
# -----------------------------
def parse_mapping(text: str) -> Dict[str, str]:
    """
    예)
    로메인=잎로메인
    바질=스위트바질
    """
    mp = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            a, b = line.split("=", 1)
            a, b = a.strip(), b.strip()
            if a:
                mp[a] = b
    return mp


def clean_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\s\n]+", "", s)
    s = re.sub(r"[^0-9A-Za-z가-힣]+", "", s)
    return s


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def correct_with_dictionary(raw: str, dictionary: List[str], threshold: float = 0.55) -> str:
    """
    OCR 결과(raw)가 이상하면, dictionary 중 가장 비슷한 상품명으로 보정
    """
    raw = clean_name(raw)
    if not raw or not dictionary:
        return raw

    best = raw
    best_score = 0.0
    for cand in dictionary:
        cand2 = clean_name(cand)
        if not cand2:
            continue
        sc = similarity(raw, cand2)
        if sc > best_score:
            best_score = sc
            best = cand2

    return best if best_score >= threshold else raw


# -----------------------------
# 빨간 라벨 탐지
# -----------------------------
def detect_red_label_boxes(bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

    # 빨강은 HSV에서 양 끝에 걸려서 2구간
    lower1 = np.array([0, 70, 70])
    upper1 = np.array([10, 255, 255])
    lower2 = np.array([170, 70, 70])
    upper2 = np.array([180, 255, 255])

    mask = cv2.inRange(hsv, lower1, upper1) | cv2.inRange(hsv, lower2, upper2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        ar = w / max(1, h)

        # 라벨 스티커 크기 필터 (사진마다 약간 조정 가능)
        if area < 2500 or area > 70000:
            continue
        if ar < 1.2 or ar > 6.5:
            continue

        boxes.append((x, y, w, h))

    # 화면에서 보기 좋게 위→아래, 좌→우 정렬
    boxes = sorted(boxes, key=lambda b: (b[1] // 20, b[0]))
    return boxes


def find_right_boundary(box: Tuple[int, int, int, int], boxes: List[Tuple[int, int, int, int]], img_w: int) -> int:
    x, y, w, h = box
    y1, y2 = y, y + h

    candidates = []
    for xb, yb, wb, hb in boxes:
        if xb <= x:
            continue
        ov = max(0, min(y2, yb + hb) - max(y1, yb))
        if ov / max(1, min(h, hb)) >= 0.4:
            candidates.append(xb)

    return min(candidates) if candidates else img_w


# -----------------------------
# OCR (상품명 / 재고)
# -----------------------------
def preprocess_otsu(gray: np.ndarray, scale: int = 4) -> np.ndarray:
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return th


def preprocess_adaptive(gray: np.ndarray, scale: int = 3) -> np.ndarray:
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 31, 15)
    return th


def ocr_name_from_box(bgr: np.ndarray, box: Tuple[int, int, int, int], dictionary: List[str]) -> str:
    x, y, w, h = box
    pad = 12
    crop = bgr[y + pad:y + h - pad, x + pad:x + w - pad]
    if crop.size == 0:
        return ""

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    # 2가지 전처리 결과를 만들어서 더 그럴듯한 걸 선택
    th1 = preprocess_otsu(gray, scale=4)
    th2 = preprocess_adaptive(gray, scale=3)

    cand1 = clean_name(pytesseract.image_to_string(th1, lang="kor", config="--oem 1 --psm 7"))
    cand2 = clean_name(pytesseract.image_to_string(th2, lang="kor", config="--oem 1 --psm 7"))

    # 사전이 있으면: 사전에 가장 잘 맞는 후보를 선택
    if dictionary:
        c1 = correct_with_dictionary(cand1, dictionary)
        c2 = correct_with_dictionary(cand2, dictionary)

        # 원본 후보가 사전에 얼마나 잘 맞는지 점수 비교
        def best_score(raw: str) -> float:
            if not raw:
                return 0.0
            return max((similarity(raw, d) for d in dictionary), default=0.0)

        return c1 if best_score(c1) >= best_score(c2) else c2

    # 사전이 없으면: 더 “길고 한글 포함” 같은 쪽 선호
    return cand1 if len(cand1) >= len(cand2) else cand2


def ocr_qty_robust(roi_bgr: np.ndarray) -> str:
    if roi_bgr.size == 0:
        return ""

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    th = preprocess_otsu(gray, scale=4)

    txt = pytesseract.image_to_string(th, lang="eng", config="--oem 1 --psm 6").strip()
    txt = txt.replace(" ", "").replace("\n", "")

    # 자주 틀리는 문자 교정 (9→Q 같이)
    trans = str.maketrans({"Q": "9", "O": "0", "o": "0", "S": "5", "I": "1", "l": "1", "Z": "2", "B": "8"})
    txt = txt.translate(trans)

    m = re.search(r"(\d+(\.\d+)?[kK]?)", txt)
    return m.group(1) if m else ""


def get_qty_roi(bgr: np.ndarray, box: Tuple[int, int, int, int], boxes: List[Tuple[int, int, int, int]]) -> np.ndarray:
    img_h, img_w = bgr.shape[:2]
    x, y, w, h = box

    x_start = x + w + 12
    x_end = find_right_boundary(box, boxes, img_w) - 12
    if x_end <= x_start:
        x_end = min(img_w, x_start + 220)

    y_start = max(0, y - 6)
    y_end = min(img_h, y + h + 6)

    return bgr[y_start:y_end, x_start:x_end]


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="재고 인식", layout="wide")
st.title("빨간 라벨(이름표)만 인식해서 재고 표시 + 상품명 변경")

col1, col2 = st.columns([1, 1])

with col1:
    image_path = st.text_input("이미지 경로(업로드 없이 로컬 파일 경로)", value="재고.jpg")
    tesseract_path = st.text_input("Windows라면 tesseract.exe 경로(비우면 자동)", value="")
    show_debug = st.checkbox("디버그(라벨 박스 표시)", value=True)

    st.caption("※ 예: C:\\Program Files\\Tesseract-OCR\\tesseract.exe")

with col2:
    dictionary_text = st.text_area(
        "상품명 사전(선택) — 한 줄에 하나씩. OCR이 틀려도 여기 목록으로 자동 보정",
        value="고수\n공심채\n빈스\n당귀\n딜\n라디치오\n적환\n로즈마리\n로케트\n모둠\n바질\n베타인\n쏙세러리\n송배추\n애플\n와일드\n로메인\n적겨자\n적근대\n적양파\n쪽리커리\n청경채\n치커리\n케일\n타임\n통로메인\n향나물\n차빌\n",
        height=220
    )
    rename_text = st.text_area(
        "표시명 변경 규칙(선택) — 원본=변경 (한 줄에 1개)",
        value="로메인=잎로메인",
        height=120
    )

# tesseract 경로 설정
if tesseract_path.strip():
    pytesseract.pytesseract.tesseract_cmd = tesseract_path.strip()

# 이미지 로드
try:
    pil = Image.open(image_path).convert("RGB")
    bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
except Exception as e:
    st.error(f"이미지를 열 수 없어요: {e}")
    st.stop()

boxes = detect_red_label_boxes(bgr)

# 디버그: 박스 표시
if show_debug:
    dbg = bgr.copy()
    for i, (x, y, w, h) in enumerate(boxes):
        cv2.rectangle(dbg, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(dbg, str(i), (x, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    st.image(cv2.cvtColor(dbg, cv2.COLOR_BGR2RGB), caption="감지된 빨간 라벨(초록 박스)")

dictionary = [clean_name(x) for x in dictionary_text.splitlines() if clean_name(x)]
rename_map = parse_mapping(rename_text)

rows = []
for box in boxes:
    raw_name = ocr_name_from_box(bgr, box, dictionary=dictionary)
    if not raw_name:
        continue

    # 사전 보정(최종 상품명)
    name = correct_with_dictionary(raw_name, dictionary) if dictionary else raw_name

    # 표시명 변경
    display_name = rename_map.get(name, name)

    qty_roi = get_qty_roi(bgr, box, boxes)
    qty = ocr_qty_robust(qty_roi)

    # 이름만 있고 수량이 빈칸인 경우도 있을 수 있음(체크박스만 있는 줄 등)
    rows.append({"원본상품명": name, "표시상품명": display_name, "재고": qty})

df = pd.DataFrame(rows)

st.subheader("재고 결과")
if df.empty:
    st.warning("라벨은 찾았는데 OCR 결과가 비었어요. (빛반사/흐림/경로/테서랙트 설정 확인)")
else:
    # 화면에 보여줄 형태: 표시상품명 / 재고
    out = df[["표시상품명", "재고"]].copy()
    out.columns = ["상품명", "재고"]
    st.dataframe(out, use_container_width=True)

    st.subheader("텍스트 출력(복사해서 사용)")
    lines = ["상품명 재고"] + [f"{r['상품명']} {r['재고']}".strip() for _, r in out.iterrows()]
    st.code("\n".join(lines), language="text")

    with st.expander("디버그: 원본상품명/표시상품명/재고 전체"):
        st.dataframe(df, use_container_width=True)
