import io
import os
import re
from collections import defaultdict

import pandas as pd
import streamlit as st

# -------------------- PDF export (reportlab) --------------------
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle

# -------------------- PDF text extract libs --------------------
try:
    import pdfplumber  # pip install pdfplumber
except Exception:
    pdfplumber = None

try:
    from pypdf import PdfReader  # pip install pypdf
except Exception:
    try:
        from PyPDF2 import PdfReader  # fallback
    except Exception:
        PdfReader = None


# ==================== 설정(필요하면 여기만 바꾸면 됨) ====================
RULES_FILE = "rules.txt"
FONT_PATH = os.path.join("fonts", "NanumGothic.ttf")  # 레포에 올린 경로 그대로
FONT_NAME = "NanumGothic"

# 팩은 정수로만 표시(예: 2팩), 박스는 소수 허용(예: 1.75박스)
ALLOW_DECIMAL_PACK = False
ALLOW_DECIMAL_BOX = True

COUNT_UNITS = ["개", "통", "팩", "봉"]
# ======================================================================


# -------------------- Rules helpers --------------------
def norm_type(t: str) -> str:
    t = (t or "").strip()
    if t in ["팩", "PACK", "pack", "Pack"]:
        return "PACK"
    if t in ["박스", "BOX", "box", "Box"]:
        return "BOX"
    if t in ["구분", "분리", "DETAIL", "detail", "SPEC", "spec"]:
        return "DETAIL"
    return t.upper().strip()


def parse_pack_size_g(val: str) -> float:
    v = (val or "").strip().lower().replace(" ", "")
    if v.endswith("kg"):
        return float(v[:-2]) * 1000.0
    if v.endswith("g"):
        return float(v[:-1])
    return float(v)


def parse_box_size_kg(val: str) -> float:
    v = (val or "").strip().lower().replace(" ", "")
    if v.endswith("g"):
        return float(v[:-1]) / 1000.0
    if v.endswith("kg"):
        return float(v[:-2])
    return float(v)


def load_rules_text() -> str:
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def parse_rules(text: str):
    """
    rules.txt 예시:
      PACK,건대추,500
      BOX,적겨자,2
      DETAIL,샬롯,1   (또는 구분,샬롯,1)
    """
    pack_rules = {}
    box_rules = {}
    detail_products = set()

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue

        typ = norm_type(parts[0])
        name = parts[1].strip()
        val_raw = parts[2].strip() if len(parts) >= 3 else ""

        try:
            if typ == "PACK":
                if not val_raw:
                    continue
                size_g = parse_pack_size_g(val_raw)
                if size_g > 0:
                    pack_rules[name] = {"size_g": size_g}

            elif typ == "BOX":
                if not val_raw:
                    continue
                size_kg = parse_box_size_kg(val_raw)
                if size_kg > 0:
                    box_rules[name] = {"size_kg": size_kg}

            elif typ == "DETAIL":
                detail_products.add(name)

        except Exception:
            continue

    return pack_rules, box_rules, detail_products


# -------------------- PDF parsing --------------------
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
        raise RuntimeError("pdfplumber 또는 pypdf(PyPDF2)가 필요합니다. (requirements.txt 확인)")

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
    반환: (제품명, 구분텍스트, 수량)
    - "샬롯 1kg 3" 같은 한 줄 형태
    - "샬롯 1kg" 다음 줄 "3" 형태 모두 처리
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

        # 끝에 수량이 붙은 줄
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

        # 다음 줄 수량 대기
        toks = ln.split()
        product = toks[0]
        spec = " ".join(toks[1:]) if len(toks) > 1 else ""
        pending = (product, spec)

    return items


def parse_spec_components(spec: str):
    if not spec:
        return None

    s = spec.replace(",", "").replace(" ", "")
    s = s.replace("㎏", "kg").replace("ＫＧ", "kg").replace("KG", "kg").lower()

    out = {"grams_per_unit": None, "bunch_per_unit": None, "counts_per_unit": {}}

    mw = re.search(r"(\d+(?:\.\d+)?)(kg|g)", s)
    if mw:
        num = float(mw.group(1))
        unit = mw.group(2)
        out["grams_per_unit"] = num * 1000.0 if unit == "kg" else num

    mb = re.search(r"(\d+)단", s)
    if mb:
        out["bunch_per_unit"] = int(mb.group(1))

    for u in COUNT_UNITS:
        mu = re.search(r"(\d+)" + re.escape(u), s)
        if mu:
            out["counts_per_unit"][u] = int(mu.group(1))

    if out["grams_per_unit"] is None and out["bunch_per_unit"] is None and not out["counts_per_unit"]:
        return None
    return out


def normalize_spec_for_detail(spec: str) -> str:
    s = (spec or "").strip()
    s = s.replace(" ", "").replace(",", "")
    s = s.replace("㎏", "kg").replace("ＫＧ", "kg").replace("KG", "kg")
    return s


def aggregate(items: list[tuple[str, str, int]]):
    agg = default
