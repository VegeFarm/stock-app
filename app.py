import io
import re
from collections import defaultdict

import pandas as pd
import streamlit as st

# pdfplumber가 있으면 우선 사용(표/텍스트 추출이 더 안정적)
try:
    import pdfplumber  # pip install pdfplumber
except Exception:
    pdfplumber = None

# pdfplumber가 없을 때 fallback
try:
    from PyPDF2 import PdfReader  # pip install pypdf2
except Exception:
    PdfReader = None


COUNT_UNITS = ["개", "통", "팩", "봉"]


def extract_lines_from_pdf(file_bytes: bytes) -> list[str]:
    lines: list[str] = []

    if pdfplumber is not None:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for ln in text.splitlines():
                    ln = ln.strip()
                    if ln:
                        lines.append(ln)
        return lines

    if PdfReader is None:
        raise RuntimeError("pdfplumber 또는 pypdf2가 필요합니다. (pip install pdfplumber pypdf2)")

    reader = PdfReader(io.BytesIO(file_bytes))
    for page in reader.pages:
        text = page.extract_text() or ""
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                lines.append(ln)
    return lines


def parse_items(lines: list[str]) -> list[tuple[str, str, int]]:
    """
    반환: (제품명, 구분(예: 500g/2단/10봉), 수량)
    PDF 추출 결과가
      - "고수 100g 2" 형태로 한 줄에 붙어있거나
      - "고수 100g" 다음 줄에 "2" 형태로 분리되어 있어도
    둘 다 처리
    """
    items: list[tuple[str, str, int]] = []
    pending: tuple[str, str] | None = None

    for ln in lines:
        if ln in ("▣ 제품별 개수", "제품명 구분 수량"):
            continue

        # 수량만 단독 줄
        if re.fullmatch(r"\d+", ln):
            if pending is not None:
                product, spec = pending
                items.append((product, spec, int(ln)))
                pending = None
            continue

        # 끝에 수량이 붙어있는 줄: "... 10"
        m = re.match(r"^(.*?)(?:\s+)(\d+)$", ln)
        if m:
            main = m.group(1).strip()
            qty = int(m.group(2))
            toks = main.split()
            product = toks[0]
            spec = " ".join(toks[1:]) if len(toks) > 1 else ""
            items.append((product, spec, qty))
            pending = None
            continue

        # 아직 수량이 안 나온 줄(다음 줄에서 수량을 받을 수 있음)
        toks = ln.split()
        product = toks[0]
        spec = " ".join(toks[1:]) if len(toks) > 1 else ""
        pending = (product, spec)

    return items


def parse_spec(spec: str):
    """
    spec 예:
      - 500g, 1kg, 1000g
      - 1단, 2단, 5단
      - 1개, 2통, 5팩, 10봉
    반환: (num, unit) or None
    """
    if not spec:
        return None

    s = spec.replace(" ", "").replace(",", "")
    s = s.replace("㎏", "kg").replace("ＫＧ", "kg").replace("KG", "kg")

    m = re.match(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>kg|g|단|개|통|팩|봉)$", s)
    if not m:
        return None
    return float(m.group("num")), m.group("unit")


def aggregate(items: list[tuple[str, str, int]]):
    agg = defaultdict(lambda: {"grams": 0, "bunch": 0, "counts": defaultdict(int), "unknown": defaultdict(int)})

    for product, spec, qty in items:
        parsed = parse_spec(spec)
        if parsed is None:
            agg[product]["unknown"][spec] += qty
            continue

        num, unit = parsed

        if unit == "kg":
            agg[product]["grams"] += int(round(num * 1000)) * qty
        elif unit == "g":
            agg[product]["grams"] += int(round(num)) * qty
        elif unit == "단":
            agg[product]["bunch"] += int(round(num)) * qty
        elif unit in COUNT_UNITS:
            agg[product]["counts"][unit] += int(round(num)) * qty
        else:
            agg[product]["unknown"][spec] += qty

    return agg


def format_weight(grams: int) -> str | None:
    if grams <= 0:
        return None
    kg = grams // 1000
    rem = grams % 1000
    if kg > 0 and rem == 0:
        return f"{kg}kg"
    if kg > 0 and rem > 0:
        return f"{kg}kg {rem}g"
    return f"{grams}g"


def format_total(rec) -> str:
    parts = []

    if rec["bunch"]:
        parts.append(f'{rec["bunch"]}단')

    w = format_weight(rec["grams"])
    if w:
        parts.append(w)

    for u in COUNT_UNITS:
        if rec["counts"].get(u, 0):
            parts.append(f'{rec["counts"][u]}{u}')

    # 파싱 못한 구분은 그대로 표시(필요 없으면 제거 가능)
    for spec, qty in rec["unknown"].items():
        if spec:
            parts.append(f"{spec}×{qty}")

    return " ".join(parts) if parts else "0"


# -------------------- Streamlit UI --------------------
st.set_page_config(page_title="제품별 수량 합산", layout="wide")
st.title("제품별 수량 합산(PDF 업로드)")

uploaded = st.file_uploader("PDF 업로드", type=["pdf"])

if uploaded:
    file_bytes = uploaded.getvalue()

    lines = extract_lines_from_pdf(file_bytes)
    items = parse_items(lines)
    agg = aggregate(items)

    rows = []
    for product in sorted(agg.keys()):
        rows.append({"제품명": product, "합계": format_total(agg[product])})

    df = pd.DataFrame(rows)

    st.subheader("제품별 합계")
    st.dataframe(df, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "CSV 다운로드",
        data=csv,
        file_name="제품별_합계.csv",
        mime="text/csv",
    )
else:
    st.caption("※ PDF가 스캔본(이미지)이라 텍스트 추출이 안 되면, OCR 처리가 추가로 필요합니다.")
