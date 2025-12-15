import io
import os
import re
from collections import defaultdict

import pandas as pd
import streamlit as st

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


# ===================== 설정 =====================
RULES_FILE = "rules.txt"
FONT_FILE = os.path.join("fonts", "NanumGothic.ttf")
FONT_NAME = "NanumGothic"

# 표시 옵션(사이드바 없으니 여기서 고정)
ALLOW_DECIMAL_PACK = False   # 팩 소수 허용 (False면 정수로만)
ALLOW_DECIMAL_BOX = True     # 박스 소수 허용

COUNT_UNITS = ["개", "통", "팩", "봉"]
# ===============================================


# -------------------- Rules --------------------
def norm_type(t: str) -> str:
    t = (t or "").strip()
    if t in ["팩", "PACK", "pack", "Pack"]:
        return "PACK"
    if t in ["박스", "BOX", "box", "Box"]:
        return "BOX"
    if t in ["구분", "DETAIL", "detail", "SPEC", "spec"]:
        return "DETAIL"
    return t.upper().strip()


def parse_pack_size_g(val: str) -> float:
    """PACK 값: 500 / 500g / 0.5kg 허용 -> g로 반환"""
    v = (val or "").strip().lower().replace(" ", "")
    if v.endswith("kg"):
        return float(v[:-2]) * 1000.0
    if v.endswith("g"):
        return float(v[:-1])
    return float(v)


def parse_box_size_kg(val: str) -> float:
    """BOX 값: 2 / 2kg / 2000g 허용 -> kg로 반환"""
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
      DETAIL,샬롯,1
      구분,미니양배추,1
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
        raise RuntimeError("pdfplumber 또는 pypdf(PyPDF2)가 필요합니다. requirements.txt 확인!")

    reader = PdfReader(io.BytesIO(file_bytes))
    for page in reader.pages:
        text = page.extract_text() or ""
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                lines.append(ln)
    return lines


def parse_items(lines: list[str]) -> list[tuple[str, str, int]]:
    items: list[tuple[str, str, int]] = []
    pendi
