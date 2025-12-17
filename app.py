import io
import os
import re
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import pandas as pd
import streamlit as st

# -------------------- Optional: AgGrid (one-table edit + conditional color) --------------------
try:
    from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode, JsCode
    try:
        # 컬럼을 화면에 맞춰 자동으로 줄여서(가로 드래그 최소화)
        from st_aggrid.shared import ColumnsAutoSizeMode
    except Exception:
        ColumnsAutoSizeMode = None
except Exception:
    AgGrid = None
    GridOptionsBuilder = None
    GridUpdateMode = None
    DataReturnMode = None
    JsCode = None
    ColumnsAutoSizeMode = None
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle

# -------------------- Pillow (merge PNG pages -> one PNG) --------------------
try:
    from PIL import Image
except Exception:
    Image = None

# -------------------- PDF image render (screenshot) --------------------
try:
    import fitz  # PyMuPDF (pymupdf)
except Exception:
    fitz = None

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

COUNT_UNITS = ["개", "통", "팩", "봉"]
RULES_FILE = "rules.txt"

# ✅ 한국시간(KST) 고정(서버가 UTC여도 파일명은 한국시간)
KST = timezone(timedelta(hours=9))


def now_prefix_kst() -> str:
    return datetime.now(KST).strftime("%Y%m%d_%H%M%S")


# ✅ 제품별 합계 고정 순서(표에 항상 먼저, 위→아래 기준)
FIXED_PRODUCT_ORDER = [
    "고수",
    "공심채",
    "그린빈",
    "당귀잎",
    "딜",
    "래디쉬",
    "로즈마리",
    "로케트",
    "바질",
    "로즈잎",
    "비타민",
    "쌈샐러리",
    "쌈추",
    "애플민트",
    "와일드",
    "잎로메인",
    "적겨자",
    "적근대",
    "적치커리",
    "청경채",
    "청치커리",
    "케일",
    "타임",
    "통로메인",
    "향나물",
    "뉴그린",
    "처빌",
]


# -------------------- Rules helpers --------------------
def norm_type(t: str) -> str:
    t = (t or "").strip()
    if t in ["팩", "PACK", "pack", "Pack"]:
        return "PACK"
    if t in ["박스", "BOX", "box", "Box"]:
        return "BOX"
    if t in ["개", "EA", "ea", "Each", "EACH"]:
        return "EA"
    return t.upper().strip()


def display_type(typ: str) -> str:
    typ = norm_type(typ)
    return {"PACK": "팩", "BOX": "박스", "EA": "개"}.get(typ, typ)


def parse_pack_size_g(val: str) -> float:
    """(PACK/EA) 값: 500 / 500g / 0.5kg 허용 -> g로 반환"""
    v = (val or "").strip().lower().replace(" ", "")
    if v.endswith("kg"):
        return float(v[:-2]) * 1000.0
    if v.endswith("g"):
        return float(v[:-1])
    return float(v)


def parse_box_size_kg(val: str) -> float:
    """(BOX) 값: 2 / 2kg / 2000g 허용 -> kg로 반환"""
    v = (val or "").strip().lower().replace(" ", "")
    if v.endswith("g"):
        return float(v[:-1]) / 1000.0
    if v.endswith("kg"):
        return float(v[:-2])
    return float(v)


def load_rules_text() -> str:
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass

    return """# TYPE,상품명,값
# 팩(PACK),상품명,팩_기준_g(=1팩이 몇 g인지)  ex) 500 / 500g / 0.5kg
# 박스(BOX),상품명,박스_기준_kg(=1박스가 몇 kg인지) ex) 2 / 2kg / 2000g
# 개(EA),상품명,1개_기준_g(=1개가 몇 g인지) ex) 1kg / 500g
#
# ✅ 출력 규칙
# - 화면/결과는 모두 숫자만 출력(단위 글자 없음)
# - BOX 등록 상품은 1 미만이어도 나눠서 표시 (예: 600g / 2000g = 0.3)

팩,건대추,500
팩,양송이,500

박스,적겨자,2
박스,적근대,2

# 예) 개,깐마늘,1kg  -> 합계 10kg이면 10(숫자만)로 표시(정수일 때만)
"""


def save_rules_text(text: str) -> None:
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        f.write(text or "")


def parse_rules(text: str):
    pack_rules = {}  # {상품명: {"size_g": float}}
    box_rules = {}   # {상품명: {"size_kg": float}}
    ea_rules = {}    # {상품명: {"size_g": float}}

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue

        typ = norm_type(parts[0])
        name = parts[1].strip()
        val_raw = parts[2].strip()

        try:
            if typ == "PACK":
                size_g = parse_pack_size_g(val_raw)
                if size_g > 0:
                    pack_rules[name] = {"size_g": size_g}

            elif typ == "BOX":
                size_kg = parse_box_size_kg(val_raw)
                if size_kg > 0:
                    box_rules[name] = {"size_kg": size_kg}

            elif typ == "EA":
                size_g = parse_pack_size_g(val_raw)
                if size_g > 0:
                    ea_rules[name] = {"size_g": size_g}
        except Exception:
            continue

    return pack_rules, box_rules, ea_rules


def upsert_rule(text: str, typ: str, name: str, val: str) -> str:
    typ_norm = norm_type(typ)
    typ_disp = display_type(typ_norm)

    name = (name or "").strip()
    val = (val or "").strip()
    if not typ_norm or not name or not val:
        return text

    lines = (text or "").splitlines()
    out = []
    replaced = False

    for ln in lines:
        if ln.strip().startswith("#") or not ln.strip():
            out.append(ln)
            continue

        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 2 and norm_type(parts[0]) == typ_norm and parts[1] == name:
            out.append(f"{typ_disp},{name},{val}")
            replaced = True
        else:
            out.append(ln)

    if not replaced:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{typ_disp},{name},{val}")

    return "\n".join(out)


# -------------------- PDF -> PNG screenshots --------------------
def render_pdf_pages_to_images(file_bytes: bytes, zoom: float = 2.0) -> list[bytes]:
    """
    PDF 각 페이지를 PNG 스크린샷으로 렌더링하여 bytes 리스트 반환
    zoom: 1.0~3.5 (클수록 선명/용량 증가)
    """
    if fitz is None:
        raise RuntimeError("스크린샷 저장은 pymupdf가 필요합니다. (pip install pymupdf)")

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    out: list[bytes] = []
    mat = fitz.Matrix(zoom, zoom)

    for i in range(doc.page_count):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out.append(pix.tobytes("png"))

    doc.close()
    return out


def merge_png_pages_to_one(png_bytes_list: list[bytes]) -> bytes:
    """
    여러 PNG(페이지)를 세로로 이어붙여 1장 PNG로 반환
    Pillow(PIL) 필요
    """
    if not png_bytes_list:
        return b""

    if len(png_bytes_list) == 1:
        return png_bytes_list[0]

    if Image is None:
        # PIL 없으면 첫 페이지만 반환(그래도 'PNG 1개'는 유지)
        return png_bytes_list[0]

    imgs = [Image.open(io.BytesIO(b)).convert("RGBA") for b in png_bytes_list]
    max_w = max(im.width for im in imgs)
    total_h = sum(im.height for im in imgs)

    canvas = Image.new("RGBA", (max_w, total_h), (255, 255, 255, 0))
    y = 0
    for im in imgs:
        x = (max_w - im.width) // 2
        canvas.paste(im, (x, y))
        y += im.height

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


# -------------------- PDF text parsing --------------------
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
        raise RuntimeError("pdfplumber 또는 pypdf(PyPDF2)가 필요합니다. (pip install pdfplumber pypdf)")

    reader = PdfReader(io.BytesIO(file_bytes))
    try:
        if getattr(reader, "is_encrypted", False):
            reader.decrypt("")
    except Exception:
        pass

    for page in reader.pages:
        text = page.extract_text() or ""
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                lines.append(ln)
    return lines


def parse_items(lines: list[str]) -> list[tuple[str, str, int]]:
    items: list[tuple[str, str, int]] = []
    pending: tuple[str, str] | None = None

    for ln in lines:
        if ln in ("▣ 제품별 개수", "제품명 구분 수량"):
            continue

        if re.fullmatch(r"\d+", ln):
            if pending is not None:
                product, spec = pending
                items.append((product, spec, int(ln)))
                pending = None
            continue

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

    # ✅ 19kg250g 같은 결합 표기 지원
    m2 = re.search(r"(\d+(?:\.\d+)?)kg(\d+(?:\.\d+)?)g", s)
    if m2:
        kg = float(m2.group(1))
        g = float(m2.group(2))
        out["grams_per_unit"] = kg * 1000.0 + g
    else:
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


def aggregate(items: list[tuple[str, str, int]]):
    agg = defaultdict(lambda: {"grams": 0.0, "bunch": 0, "counts": defaultdict(int), "unknown": defaultdict(int)})

    for product, spec, qty in items:
        comp = parse_spec_components(spec)
        if comp is None:
            agg[product]["unknown"][spec] += qty
            continue

        if comp["grams_per_unit"] is not None:
            agg[product]["grams"] += comp["grams_per_unit"] * qty

        if comp["bunch_per_unit"] is not None:
            agg[product]["bunch"] += comp["bunch_per_unit"] * qty

        for unit, n in comp["counts_per_unit"].items():
            agg[product]["counts"][unit] += n * qty

    return agg


# -------------------- Formatting --------------------
def fmt_num(x: float, max_dec=2) -> str:
    s = f"{x:.{max_dec}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def format_weight(grams: float) -> str | None:
    """kg/g도 숫자만: kg 소수로 표시 (19kg250g -> 19.25)"""
    if grams <= 0:
        return None
    kg = grams / 1000.0
    return fmt_num(kg, 3)


def _append_count_parts(parts: list[str], counts: dict):
    """개/팩/통/봉 전부 숫자만"""
    for u in ["개", "팩", "통", "봉"]:
        v = counts.get(u, 0)
        if v:
            parts.append(f"{v}")


def format_total_custom(product: str, rec, pack_rules, box_rules, ea_rules,
                        allow_decimal_pack: bool, allow_decimal_box: bool) -> str:
    parts: list[str] = []

    # 단도 숫자만
    if rec["bunch"]:
        parts.append(f'{rec["bunch"]}')

    grams = rec["grams"]
    counts = dict(rec["counts"])

    # BOX 우선: 박스 기준으로 나눈 값(0.3처럼) 표시 (1 미만이어도 항상 표시)
    if product in box_rules and grams > 0:
        box_size_kg = float(box_rules[product]["size_kg"])
        denom_g = box_size_kg * 1000.0
        boxes = grams / denom_g

        if allow_decimal_box:
            parts.append(f"{fmt_num(boxes, 2)}")
        else:
            if abs(boxes - round(boxes)) < 1e-9:
                parts.append(f"{int(round(boxes))}")
            else:
                parts.append(f"{fmt_num(boxes, 2)}")

        _append_count_parts(parts, counts)
        return " ".join(parts).strip() if parts else "0"

    # PACK / EA 처리
    pack_shown = False
    ea_shown = False

    # spec 자체에 팩이 있으면 우선
    if counts.get("팩", 0) > 0:
        parts.append(f'{counts["팩"]}')
        pack_shown = True
        counts.pop("팩", None)

    # rules로 g -> 팩 변환
    elif product in pack_rules and grams > 0:
        size_g = float(pack_rules[product]["size_g"])
        packs = grams / size_g
        if allow_decimal_pack:
            parts.append(f"{fmt_num(packs, 2)}")
            pack_shown = True
        else:
            if abs(packs - round(packs)) < 1e-9:
                parts.append(f"{int(round(packs))}")
                pack_shown = True

    # 팩이 안 잡혔으면 "개" 처리
    if not pack_shown:
        if counts.get("개", 0) > 0:
            parts.append(f'{counts["개"]}')
            ea_shown = True
            counts.pop("개", None)

        elif product in ea_rules and grams > 0:
            size_g = float(ea_rules[product]["size_g"])
            eas = grams / size_g
            # 정수로 딱 떨어질 때만 표시(아니면 중량 kg 소수로)
            if abs(eas - round(eas)) < 1e-9:
                parts.append(f"{int(round(eas))}")
                ea_shown = True

    # 팩도 개도 안 잡히면 중량(kg 소수)
    if not pack_shown and not ea_shown:
        w = format_weight(grams)
        if w:
            parts.append(w)

    _append_count_parts(parts, counts)
    return " ".join(parts).strip() if parts else "0"


def to_3_per_row(df: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """
    ✅ 세로 우선 배치(위→아래), 그 다음 열로 이동
    n=3이면 1열을 위→아래로 다 채운 뒤 2열, 3열 순서
    """
    if df is None or len(df) == 0:
        row = {}
        for c in range(n):
            row[f"제품명{c+1}"] = ""
            row[f"합계{c+1}"] = ""
        return pd.DataFrame([row])

    total = len(df)
    rows_count = math.ceil(total / n)

    out = []
    for r in range(rows_count):
        row = {}
        for c in range(n):
            idx = c * rows_count + r  # ⭐ 세로 우선 핵심
            if idx < total:
                row[f"제품명{c+1}"] = df.iloc[idx]["제품명"]
                row[f"합계{c+1}"] = df.iloc[idx]["합계"]
            else:
                row[f"제품명{c+1}"] = ""
                row[f"합계{c+1}"] = ""
        out.append(row)

    return pd.DataFrame(out)


def make_pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    font_path = os.path.join("fonts", "NanumGothic.ttf")
    font_name = "NanumGothic"

    if not os.path.exists(font_path):
        raise RuntimeError(f"폰트 파일을 못 찾음: {font_path} (fonts 폴더/파일명 확인)")

    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, font_path))
        pdfmetrics.registerFontFamily(
            font_name, normal=font_name, bold=font_name, italic=font_name, boldItalic=font_name
        )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=18, rightMargin=18, topMargin=18, bottomMargin=18
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"].clone("KTitle")
    title_style.fontName = font_name

    cell_style = ParagraphStyle(
        "KCell", fontName=font_name, fontSize=10, leading=12,
        alignment=1, wordWrap="CJK"
    )
    header_style = ParagraphStyle(
        "KHeader", fontName=font_name, fontSize=10, leading=12,
        alignment=1, wordWrap="CJK"
    )

    elements = [Paragraph(title, title_style), Spacer(1, 12)]
    safe_df = df.fillna("").astype(str)

    header = [Paragraph(str(c), header_style) for c in safe_df.columns]
    body = [[Paragraph(str(v), cell_style) for v in row] for row in safe_df.values.tolist()]
    data = [header] + body

    page_w, _ = landscape(A4)
    usable_w = page_w - 36
    col_w = usable_w / max(1, len(safe_df.columns))
    col_widths = [col_w] * len(safe_df.columns)

    table = Table(data, repeatRows=1, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    elements.append(table)
    doc.build(elements)
    return buf.getvalue()



# -------------------- Streamlit UI --------------------
st.set_page_config(
    page_title="재고프로그램",
    page_icon="assets/favicon.png",  # ✅ 로고 파비콘
    layout="wide",
)

# ----- Navigation -----
if "page" not in st.session_state:
    st.session_state["page"] = "pdf_sum"

with st.sidebar:
    st.markdown("## 📌 메뉴")
    if st.button("📄 PDF 제품별합계", use_container_width=True):
        st.session_state["page"] = "pdf_sum"
        st.rerun()
    if st.button("📦 재고관리", use_container_width=True):
        st.session_state["page"] = "inventory"
        st.rerun()
    st.divider()


INVENTORY_FILE = "inventory.csv"

INVENTORY_COLUMNS = [
    "상품명",
    "재고",
    "입고",
    "보유수량",
    "1차",
    "2차",
    "3차",
    "주문수량",
    "남은수량",
]


def _coerce_num_series(s: pd.Series) -> pd.Series:
    """숫자/소수 허용 (빈값/문자 -> 0)"""
    return pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float)


def compute_inventory_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # 기본 스키마 보정
    if "상품명" not in df.columns:
        df.insert(0, "상품명", "")

    for col in ["재고", "입고", "1차", "2차", "3차"]:
        if col not in df.columns:
            df[col] = 0

    # 숫자 정리(소수 허용)
    for col in ["재고", "입고", "1차", "2차", "3차"]:
        df[col] = _coerce_num_series(df[col])

    # 공백 상품명 정리
    df["상품명"] = df["상품명"].fillna("").astype(str).str.strip()

    df["보유수량"] = df["재고"] + df["입고"]
    df["주문수량"] = df["1차"] + df["2차"] + df["3차"]
    df["남은수량"] = df["보유수량"] - df["주문수량"]

    return df[INVENTORY_COLUMNS]


def sort_inventory_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    fixed = FIXED_PRODUCT_ORDER
    fixed_index = {name: i for i, name in enumerate(fixed)}

    def _rank(name: str) -> int:
        return fixed_index.get(name, 10_000)

    df["__rank"] = df["상품명"].apply(lambda x: _rank(str(x).strip()))
    # 고정목록 먼저, 나머지는 상품명 가나다
    df = df.sort_values(by=["__rank", "상품명"], kind="mergesort").drop(columns=["__rank"])
    return df


def load_inventory_df() -> pd.DataFrame:
    # 1) 파일 있으면 로드
    if os.path.exists(INVENTORY_FILE):
        try:
            df = pd.read_csv(INVENTORY_FILE, encoding="utf-8-sig")
        except Exception:
            df = pd.read_csv(INVENTORY_FILE, encoding="utf-8", errors="ignore")
    else:
        df = pd.DataFrame({"상품명": FIXED_PRODUCT_ORDER})

    # 2) 고정 상품이 빠져있으면 추가
    existing = set(df.get("상품명", pd.Series(dtype=str)).fillna("").astype(str).str.strip())
    missing = [p for p in FIXED_PRODUCT_ORDER if p not in existing]
    if missing:
        df = pd.concat([df, pd.DataFrame({"상품명": missing})], ignore_index=True)

    df = compute_inventory_df(df)
    df = sort_inventory_df(df)

    # 3) 완전히 빈 상품명 행 제거
    df = df[df["상품명"].astype(str).str.strip() != ""].reset_index(drop=True)
    return df


def save_inventory_df(df: pd.DataFrame) -> None:
    # 저장은 계산된 전체 컬럼 그대로 저장
    df.to_csv(INVENTORY_FILE, index=False, encoding="utf-8-sig")


def parse_sum_to_number(total_str: str) -> float:
    """제품별합계 '합계' 문자열에서 첫 번째 숫자만 뽑아 등록용 수치로 사용"""
    s = (total_str or "").strip()
    nums = re.findall(r"[-+]?\d*\.?\d+", s)
    if not nums:
        return 0.0
    try:
        return float(nums[0])
    except Exception:
        return 0.0


def register_sum_to_inventory(sum_df_long: pd.DataFrame, target_col: str, add_mode: bool = False):
    """제품별합계(df_long)를 재고관리의 1차/2차/3차 중 하나로 등록(상품명이 있는 것만)"""
    if sum_df_long is None or len(sum_df_long) == 0:
        return 0, []

    # 현재 세션에 재고표가 있으면 우선 사용, 없으면 파일에서 로드
    if "inventory_df" in st.session_state:
        inv = st.session_state["inventory_df"].copy()
    else:
        inv = load_inventory_df()

    inv = compute_inventory_df(inv)

    inv_names = inv["상품명"].fillna("").astype(str).str.strip()
    name_to_idx = {n: i for i, n in enumerate(inv_names)}

    skipped = []
    updated = 0

    for _, r in sum_df_long.iterrows():
        name = str(r.get("제품명", "")).strip()
        if not name:
            continue
        if name not in name_to_idx:
            skipped.append(name)
            continue

        qty = parse_sum_to_number(str(r.get("합계", "0")))
        i = name_to_idx[name]

        if add_mode:
            inv.at[i, target_col] = float(inv.at[i, target_col]) + float(qty)
        else:
            inv.at[i, target_col] = float(qty)

        updated += 1

    inv = compute_inventory_df(inv)
    inv = sort_inventory_df(inv).reset_index(drop=True)

    st.session_state["inventory_df"] = inv
    save_inventory_df(inv)

    return updated, skipped


def inventory_df_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    """재고표를 XLSX 바이트로 변환.

    Streamlit Cloud에서 openpyxl 미설치로 ModuleNotFoundError가 나는 경우가 있어,
    엔진을 순차 시도(openpyxl -> xlsxwriter)하도록 처리.
    둘 다 없으면 ModuleNotFoundError를 그대로 올린다.
    """

    last_err: Exception | None = None
    for engine in ("openpyxl", "xlsxwriter"):
        buf = io.BytesIO()
        try:
            with pd.ExcelWriter(buf, engine=engine) as writer:
                df.to_excel(writer, index=False, sheet_name="재고표")
                # openpyxl일 때만 시트 조작(없으면 건너뜀)
                ws = getattr(writer, "sheets", {}).get("재고표")
                if ws is not None:
                    try:
                        ws.freeze_panes = "B2"
                        widths = {
                            "A": 16, "B": 8, "C": 8, "D": 10,
                            "E": 8, "F": 8, "G": 8, "H": 10, "I": 10
                        }
                        for col, w in widths.items():
                            ws.column_dimensions[col].width = w
                    except Exception:
                        # 엔진/버전 차이로 실패해도 파일 생성은 유지
                        pass
            return buf.getvalue()
        except ModuleNotFoundError as e:
            last_err = e
            continue
        except Exception as e:
            # 다른 예외는 그대로 전달
            raise

    # 둘 다 미설치
    if isinstance(last_err, ModuleNotFoundError):
        raise last_err
    raise ModuleNotFoundError("엑셀 저장용 엔진(openpyxl/xlsxwriter)을 찾을 수 없습니다.")


def style_inventory_table(df: pd.DataFrame):
    """재고표(보기 탭) 가독성 스타일.

    - 상품명/남은수량: 크게 + 두껍게
    - 보유수량: 두껍게
    - 남은수량 조건부 색상
        * 0 미만: 빨강
        * 0 이상 10 이하: 노랑
        * 10 초과 30 미만: 색 없음
        * 30 이상: 파랑
    """
    df = df.copy()

    def _remain_style(val):
        try:
            v = float(val)
        except Exception:
            return ""
        if v < 0:
            return "background-color: #ffcccc; font-weight: 900;"  # < 0 : 빨강
        if 0 <= v <= 10:
            return "background-color: #ffe4ea; font-weight: 900;"  # 0~10 : 연분홍
        if v >= 30:
            return "background-color: #d7ecff; font-weight: 900;"  # >=30 : 연파랑
        return ""

    num_cols = [c for c in INVENTORY_COLUMNS if c != "상품명"]

    sty = df.style.applymap(_remain_style, subset=["남은수량"])
    # 숫자 표시는 보기 좋게(뒤 0 제거)
    fmt_g = lambda x: ("%g" % x) if isinstance(x, (int, float)) else x
    sty = sty.format({c: fmt_g for c in num_cols})

    # 가독성: 핵심 컬럼 강조
    sty = sty.set_properties(subset=["상품명"], **{"font-weight": "900", "font-size": "18px", "text-align": "left"})
    sty = sty.set_properties(subset=["남은수량"], **{"font-size": "18px"})
    sty = sty.set_properties(subset=["보유수량"], **{"font-weight": "900"})
    sty = sty.set_properties(subset=num_cols, **{"text-align": "right"})

    # 헤더/패딩
    sty = sty.set_table_styles([
        {"selector": "th", "props": [("font-weight", "800"), ("text-align", "center"), ("background-color", "#f3f4f6")]},
        {"selector": "td", "props": [("padding", "6px 10px")]},
    ])
    return sty


def render_inventory_page():
    st.title("재고관리")

    # 최초 로드
    if "inventory_df" not in st.session_state:
        st.session_state["inventory_df"] = load_inventory_df()
    if "inv_search" not in st.session_state:
        st.session_state["inv_search"] = ""

    def _clear_search():
        st.session_state["inv_search"] = ""

    # 원본 불러와 계산/정렬
    base = compute_inventory_df(st.session_state["inventory_df"]).copy()
    base = sort_inventory_df(base).reset_index(drop=True)

    # ---- 검색바 (검색 시에만 합계 표시) ----
    colS, colB = st.columns([4, 1])
    with colS:
        st.text_input("🔎 상품명 검색", key="inv_search", placeholder="예: 잎로메인")
    with colB:
        st.button(
            "↩ 전체보기",
            use_container_width=True,
            on_click=_clear_search,
            disabled=(st.session_state["inv_search"].strip() == ""),
        )

    q = st.session_state["inv_search"].strip()

    base_with_row = base.reset_index(drop=False).rename(columns={"index": "_row"})

    def _filter_df(df_in: pd.DataFrame) -> pd.DataFrame:
        if not q:
            return df_in
        return df_in[df_in["상품명"].astype(str).str.contains(q, case=False, na=False)].copy()

    df_display = _filter_df(base_with_row)

    # AgGrid에서 '행 추가'를 표 안에서 할 수 있도록, 항상 맨 아래에 빈 행 1개를 붙입니다.
    def _ensure_one_blank_row(df_in: pd.DataFrame) -> pd.DataFrame:
        if df_in is None:
            return df_in
        df2 = df_in.copy()
        # 마지막 행이 이미 빈 상품명이라면 추가하지 않음
        if len(df2) > 0:
            last_name = str(df2.iloc[-1].get("상품명", "")).strip()
            if last_name == "":
                return df2
        blank = {c: 0 for c in INVENTORY_COLUMNS}
        blank["상품명"] = ""
        blank["_row"] = float("nan")
        df2 = pd.concat([df2, pd.DataFrame([blank])], ignore_index=True)
        df2 = compute_inventory_df(df2)
        return df2

    df_display = _ensure_one_blank_row(df_display)

    # 표 컬럼 순서를 고정(열 위치 유지)하고, 내부용 _row는 마지막으로 보냄
    desired_cols = [
        "상품명",
        "재고",
        "입고",
        "보유수량",
        "1차",
        "2차",
        "3차",
        "주문수량",
        "남은수량",
        "_row",
    ]
    df_display = df_display[[c for c in desired_cols if c in df_display.columns]]

    # 검색 중일 때만 합계 카드 표시
    if q:
        # df_display는 _row가 포함되어 있으므로, 합계는 실제 컬럼만 기준
        c1, c2, c3 = st.columns(3)
        c1.metric("총 보유수량", fmt_num(float(df_display["보유수량"].sum()), 2))
        c2.metric("총 주문수량", fmt_num(float(df_display["주문수량"].sum()), 2))
        c3.metric("총 남은수량", fmt_num(float(df_display["남은수량"].sum()), 2))

    st.markdown("### 재고표 (수정/추가/삭제 가능)")
    st.caption("보유수량/주문수량/남은수량은 자동 계산됩니다.")

    # 공통: 숫자 컬럼 폭/표 높이(가로 드래그 최소화, 가능한 한 한 화면에)
    def _calc_height(n_rows: int) -> int:
        # 내부 스크롤(드래그) 최소화: 가능한 한 행 수만큼 높이를 키움
        # (그래도 너무 커지는 건 방지)
        return max(280, min(3000, 110 + int(n_rows) * 34))

    # ✅ 1) AgGrid가 설치되어 있으면: 한 표에서 '편집 + 조건부 색상(남은수량)'까지 완성
    if AgGrid is not None:
        # AgGrid에서 컬럼 자동맞춤 + 숫자 폭 작게 + 핵심 컬럼 강조
        remain_style = JsCode(
            """
            function(params) {
                const v = Number(params.value);
                let style = { fontWeight: '900', fontSize: '16px' };
                if (isNaN(v)) { return style; }
                if (v < 0) { style.backgroundColor = '#ffcccc'; return style; }          // < 0 : 빨강
                if (v <= 10) { style.backgroundColor = '#ffe4ea'; return style; }        // 0~10 : 연분홍
                if (v >= 30) { style.backgroundColor = '#d7ecff'; return style; }        // >=30 : 연파랑
                return style;                                                           // 10초과~30미만 : 색 없음
            }
            """
        )

        name_style = JsCode("function(params){ return { fontWeight:'900', fontSize:'16px' }; }")
        bold_style = JsCode("function(params){ return { fontWeight:'900' }; }")

        gb = GridOptionsBuilder.from_dataframe(df_display)

        # 기본 옵션
        gb.configure_default_column(
            editable=True,
            resizable=True,
            sortable=False,
            filter=False,
        )
        gb.configure_grid_options(
            rowSelection="multiple",
            suppressHorizontalScroll=True,   # 가로 드래그 최소화
            domLayout="autoHeight",         # 표 내부 스크롤 최소화(페이지 스크롤로)
        )

        # 숨김/비활성 컬럼
        gb.configure_column("_row", header_name="", hide=True, editable=False)

        # 컬럼별 설정(요청: 숫자 열 폭을 절반 정도로)
        gb.configure_column("상품명", width=200, editable=True, cellStyle=name_style)

        # 숫자열 폭: 더 좁게(가로 드래그 없이 한 화면 목표)
        num_small_w = 56
        gb.configure_column("재고", width=num_small_w, editable=True, type=["numericColumn"])
        gb.configure_column("입고", width=num_small_w, editable=True, type=["numericColumn"])
        gb.configure_column("1차", width=num_small_w, editable=True, type=["numericColumn"])
        gb.configure_column("2차", width=num_small_w, editable=True, type=["numericColumn"])
        gb.configure_column("3차", width=num_small_w, editable=True, type=["numericColumn"])

        # 자동계산(편집 불가) + 강조
        gb.configure_column("보유수량", width=num_small_w, editable=False, type=["numericColumn"], cellStyle=bold_style)
        gb.configure_column("주문수량", width=num_small_w, editable=False, type=["numericColumn"])
        gb.configure_column("남은수량", width=76, editable=False, type=["numericColumn"], cellStyle=remain_style)

        # 컬럼 순서는 df_display의 열 순서를 그대로 따릅니다.
        # (st_aggrid GridOptionsBuilder 내부 구현상 columnDefs를 리스트로 직접 넣으면
        #  일부 버전에서 AttributeError가 발생할 수 있어, 여기서는 사용하지 않습니다.)

        aggrid_kwargs = dict(
            gridOptions=gb.build(),
            data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
            update_mode=GridUpdateMode.VALUE_CHANGED,
            fit_columns_on_grid_load=True,
            allow_unsafe_jscode=True,
            height=_calc_height(len(df_display)),
            theme="streamlit",
        )
        if ColumnsAutoSizeMode is not None:
            # 모든 컬럼을 화면에 맞춰 한 번에 보이도록(버전별 상수명 차이 대응)
            _mode = None
            if hasattr(ColumnsAutoSizeMode, "FIT_ALL_COLUMNS_TO_VIEW"):
                _mode = ColumnsAutoSizeMode.FIT_ALL_COLUMNS_TO_VIEW
            elif hasattr(ColumnsAutoSizeMode, "FIT_CONTENTS"):
                _mode = ColumnsAutoSizeMode.FIT_CONTENTS
            if _mode is not None:
                aggrid_kwargs["columns_auto_size_mode"] = _mode

        grid = AgGrid(df_display, **aggrid_kwargs)

        edited_df = pd.DataFrame(grid.get("data", []))
        if edited_df.empty:
            edited_df = df_display.copy()

        # 숫자 보정/자동계산 다시 적용
        edited_df = compute_inventory_df(edited_df)

        # ---- 저장/삭제/초기화/다운로드 ----
        colA, colB, colC = st.columns([1, 1, 1])

        # 삭제: 선택 행(_row 기준) 제거
        # st_aggrid 버전별로 selected_rows가 list / DataFrame / None 등으로 들어올 수 있어
        # pandas 객체에 대해 truthiness(or [])를 평가하면 ValueError(ambiguous) 가 나므로 안전 처리
        selected_raw = grid.get("selected_rows", None)
        if selected_raw is None:
            selected = []
        elif isinstance(selected_raw, list):
            selected = selected_raw
        elif isinstance(selected_raw, pd.DataFrame):
            selected = selected_raw.to_dict("records")
        else:
            try:
                selected = list(selected_raw)
            except Exception:
                selected = []

        with colA:
            if st.button("🗑 선택 삭제", use_container_width=True, disabled=(len(selected) == 0)):
                drop_rows = []
                for r in selected:
                    try:
                        if r.get("_row") is not None and str(r.get("_row")).strip() != "":
                            drop_rows.append(int(float(r["_row"])))
                    except Exception:
                        continue

                base2 = base.copy().reset_index(drop=True)
                if drop_rows:
                    base2 = base2.drop(index=drop_rows, errors="ignore").reset_index(drop=True)

                st.session_state["inventory_df"] = compute_inventory_df(base2)
                save_inventory_df(st.session_state["inventory_df"])
                st.success("선택 행 삭제 완료!")
                st.rerun()

        with colB:
            if st.button("💾 저장", use_container_width=True):
                # base에 편집분 반영 (필터 상태여도 _row로 반영)
                base2 = base.copy().reset_index(drop=True)

                # 기존행 반영
                for _, row in edited_df.iterrows():
                    try:
                        row_id = row.get("_row")
                        if row_id is None or (isinstance(row_id, float) and math.isnan(row_id)):
                            continue
                        idx = int(float(row_id))
                        if 0 <= idx < len(base2):
                            for c in ["상품명", "재고", "입고", "1차", "2차", "3차"]:
                                if c in row:
                                    base2.at[idx, c] = row[c]
                    except Exception:
                        continue

                # 새 행(필터 중 추가) 처리: _row가 비어있는 행들
                new_rows = edited_df[edited_df["_row"].isna()].copy() if "_row" in edited_df.columns else pd.DataFrame()
                if not new_rows.empty:
                    new_rows = new_rows.drop(columns=["_row"], errors="ignore")
                    # 빈 상품명은 제외
                    new_rows["상품명"] = new_rows["상품명"].astype(str).str.strip()
                    new_rows = new_rows[new_rows["상품명"] != ""]
                    if not new_rows.empty:
                        base2 = pd.concat([base2, new_rows], ignore_index=True)

                base2 = compute_inventory_df(base2)
                base2 = sort_inventory_df(base2).reset_index(drop=True)

                st.session_state["inventory_df"] = base2
                save_inventory_df(base2)
                st.success("저장 완료!")

        with colC:
            if st.button("↻ 초기화(0으로)", use_container_width=True):
                base2 = pd.DataFrame({"상품명": FIXED_PRODUCT_ORDER})
                base2 = compute_inventory_df(base2)
                base2 = sort_inventory_df(base2).reset_index(drop=True)
                st.session_state["inventory_df"] = base2
                save_inventory_df(base2)
                st.success("초기화 완료!")
                st.rerun()

        # 다운로드(엑셀 있으면 xlsx, 없으면 csv)
        colD, colE = st.columns([1, 1])
        with colD:
            try:
                xlsx_bytes = inventory_df_to_xlsx_bytes(st.session_state["inventory_df"])
                st.download_button(
                    "⬇️ 엑셀 다운로드(.xlsx)",
                    data=xlsx_bytes,
                    file_name=f"재고표_{now_prefix_kst()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except ModuleNotFoundError:
                csv_bytes = st.session_state["inventory_df"].to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "⬇️ CSV 다운로드(엑셀 대체)",
                    data=csv_bytes,
                    file_name=f"재고표_{now_prefix_kst()}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
                st.info("엑셀(.xlsx) 다운로드는 openpyxl(또는 xlsxwriter) 설치가 필요해요. Streamlit Cloud라면 requirements.txt에 openpyxl을 추가하면 해결됩니다.")

        return

    # ✅ 2) (fallback) AgGrid 미설치 환경: 기본 DataEditor로 편집 제공(색상은 제한)
    st.info("표에서 '남은수량' 조건부 색상까지 한 번에 보려면 streamlit-aggrid 설치가 필요합니다. (requirements.txt에 streamlit-aggrid 추가)")
    df_view = df_display.drop(columns=["_row"], errors="ignore")

    edited = st.data_editor(
        df_view,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        disabled=["보유수량", "주문수량", "남은수량"],
        height=_calc_height(len(df_view)),
        column_config={
            "상품명": st.column_config.TextColumn("상품명", required=True, width="large"),
            "재고": st.column_config.NumberColumn("재고", min_value=0, step=0.01, format="%g", width="small"),
            "입고": st.column_config.NumberColumn("입고", min_value=0, step=0.01, format="%g", width="small"),
            "보유수량": st.column_config.NumberColumn("보유수량", format="%g", width="small"),
            "1차": st.column_config.NumberColumn("1차", min_value=0, step=0.01, format="%g", width="small"),
            "2차": st.column_config.NumberColumn("2차", min_value=0, step=0.01, format="%g", width="small"),
            "3차": st.column_config.NumberColumn("3차", min_value=0, step=0.01, format="%g", width="small"),
            "주문수량": st.column_config.NumberColumn("주문수량", format="%g", width="small"),
            "남은수량": st.column_config.NumberColumn("남은수량", format="%g", width="small"),
        },
        key="inventory_editor_single",
    )

    edited = compute_inventory_df(edited)
    edited = edited[edited["상품명"].astype(str).str.strip() != ""].reset_index(drop=True)
    edited = sort_inventory_df(edited).reset_index(drop=True)

    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("💾 저장", use_container_width=True):
            st.session_state["inventory_df"] = edited
            save_inventory_df(edited)
            st.success("저장 완료!")

        if st.button("↻ 초기화(0으로)", use_container_width=True):
            base2 = pd.DataFrame({"상품명": FIXED_PRODUCT_ORDER})
            base2 = compute_inventory_df(base2)
            base2 = sort_inventory_df(base2).reset_index(drop=True)
            st.session_state["inventory_df"] = base2
            save_inventory_df(base2)
            st.success("초기화 완료!")
            st.rerun()

    with col2:
        try:
            xlsx_bytes = inventory_df_to_xlsx_bytes(edited)
            st.download_button(
                "⬇️ 엑셀 다운로드(.xlsx)",
                data=xlsx_bytes,
                file_name=f"재고표_{now_prefix_kst()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except ModuleNotFoundError:
            csv_bytes = edited.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "⬇️ CSV 다운로드(엑셀 대체)",
                data=csv_bytes,
                file_name=f"재고표_{now_prefix_kst()}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            st.info("엑셀(.xlsx) 다운로드는 openpyxl(또는 xlsxwriter) 설치가 필요해요. Streamlit Cloud라면 requirements.txt에 openpyxl을 추가하면 해결됩니다.")
def render_pdf_page():

    st.title("제품별 수량 합산(PDF 업로드)")

    if "rules_text" not in st.session_state:
        st.session_state["rules_text"] = load_rules_text()

    # 기본값
    allow_decimal_pack = False
    allow_decimal_box = True

    with st.sidebar:
        st.subheader("⚙️ 표현 규칙(기본값 + 수정 가능)")

        with st.expander("🧩 PACK/BOX/EA 규칙", expanded=False):
            up = st.file_uploader("rules.txt 업로드(선택)", type=["txt"])
            if up is not None:
                st.session_state["rules_text"] = up.getvalue().decode("utf-8", errors="ignore")

            st.text_area("규칙", key="rules_text", height=260)

            colA, colB = st.columns(2)
            allow_decimal_pack = colA.checkbox("팩 소수 허용", value=False)
            allow_decimal_box = colB.checkbox("박스 소수 허용", value=True)

            with st.form("add_rule_form", clear_on_submit=False):
                st.markdown("**규칙 추가/업데이트**")
                r_type = st.selectbox("TYPE", ["팩", "개", "박스"])
                r_name = st.text_input("상품명(원본 제품명과 동일)", value="")
                r_val = st.text_input("값(PACK=1팩 g, BOX=1박스 kg, EA=1개 g)", value="")
                submitted = st.form_submit_button("추가/업데이트")
                if submitted:
                    st.session_state["rules_text"] = upsert_rule(
                        st.session_state["rules_text"], r_type, r_name, r_val
                    )
                    st.success("규칙 반영 완료!")

            col1, col2 = st.columns(2)
            if col1.button("rules.txt로 저장(로컬용)"):
                try:
                    save_rules_text(st.session_state["rules_text"])
                    st.success("rules.txt 저장 완료!")
                except Exception as e:
                    st.error(f"저장 실패: {e}")

            col2.download_button(
                "rules.txt 다운로드",
                data=st.session_state["rules_text"].encode("utf-8"),
                file_name="rules.txt",
                mime="text/plain",
            )

    pack_rules, box_rules, ea_rules = parse_rules(st.session_state["rules_text"])

    uploaded = st.file_uploader("📎 PDF 업로드", type=["pdf"])

    if uploaded:
        file_bytes = uploaded.getvalue()

        # ✅ "다운로드 시각"으로 고정되는 prefix (PDF 업로드가 바뀌면 새로 생성)
        file_sig = (uploaded.name, len(file_bytes))
        if st.session_state.get("dl_sig") != file_sig:
            st.session_state["dl_sig"] = file_sig
            st.session_state["dl_prefix"] = now_prefix_kst()
        fixed_prefix = st.session_state["dl_prefix"]

        # ---------- 원본 PDF -> 페이지별 스크린샷(PNG) 다운로드 ----------
        st.subheader("🖼️ 원본 PDF 페이지별 스크린샷 다운로드")
        try:
            zoom = 2.0
            per_row = 8  # 공간 절약(가로)

            page_images = render_pdf_pages_to_images(file_bytes, zoom=zoom)
            total = len(page_images)

            for start in range(0, total, per_row):
                cols = st.columns(per_row)
                for j in range(per_row):
                    idx = start + j
                    if idx >= total:
                        break

                    page_no = idx + 1
                    cols[j].download_button(
                        label=str(page_no),
                        data=page_images[idx],
                        file_name=f"{fixed_prefix}_{page_no}.png",
                        mime="image/png",
                        key=f"dl_img_{page_no}",
                        use_container_width=True,
                    )

        except Exception as e:
            st.error(f"스크린샷 생성 실패: {e}")

        # ---------- 제품별 합계 ----------
        lines = extract_lines_from_pdf(file_bytes)
        items = parse_items(lines)
        agg = aggregate(items)

        rows = []
        fixed_set = set(FIXED_PRODUCT_ORDER)

        # 1) 고정 상품 먼저(없으면 0)
        for product in FIXED_PRODUCT_ORDER:
            if product in agg:
                total_str = format_total_custom(
                    product, agg[product],
                    pack_rules, box_rules, ea_rules,
                    allow_decimal_pack=allow_decimal_pack,
                    allow_decimal_box=allow_decimal_box
                )
            else:
                total_str = "0"
            rows.append({"제품명": product, "합계": total_str})

        # 2) 나머지 상품 뒤에(가나다)
        rest = [p for p in agg.keys() if p not in fixed_set]
        for product in sorted(rest):
            rows.append({
                "제품명": product,
                "합계": format_total_custom(
                    product, agg[product],
                    pack_rules, box_rules, ea_rules,
                    allow_decimal_pack=allow_decimal_pack,
                    allow_decimal_box=allow_decimal_box
                ),
            })

        df_long = pd.DataFrame(rows)
        st.session_state["last_sum_df_long"] = df_long.copy()

        # ✅ 화면은 "위→아래" 순서로 보이도록 세로우선 배치
        df_wide = to_3_per_row(df_long, 3)

        st.subheader("🧾 제품별 합계")
        st.dataframe(df_wide, use_container_width=True, hide_index=True)

        # ✅ 버튼 3개를 "옆에" 배치: PDF / 스크린샷(PNG 1장) / 재고등록
        try:
            pdf_bytes = make_pdf_bytes(df_wide, "제품별 합계")

            # PDF -> PNG 페이지 렌더 -> 1장으로 합치기
            sum_imgs = render_pdf_pages_to_images(pdf_bytes, zoom=3.0)
            sum_png_one = merge_png_pages_to_one(sum_imgs)

            c1, c2, c3 = st.columns(3)
            with c1:
                st.download_button(
                    "📄 PDF 다운로드(제품별합계)",
                    data=pdf_bytes,
                    file_name="제품별_합계.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            with c2:
                st.download_button(
                    "🖼️ 스크린샷(PNG) 다운로드",
                    data=sum_png_one,
                    file_name=f"{fixed_prefix}_제품별합계.png",
                    mime="image/png",
                    use_container_width=True,
                )
            with c3:
                if st.button("📝 재고등록", use_container_width=True):
                    st.session_state["show_register_panel"] = True

            if st.session_state.get("show_register_panel"):
                st.markdown("#### 📝 재고등록 (1차/2차/3차)")
                target = st.radio("등록할 차수", ["1차", "2차", "3차"], horizontal=True, key="register_target")
                add_mode = st.checkbox("기존 값에 누적(더하기)", value=False, key="register_add_mode")

                colR1, colR2 = st.columns([1, 3])
                with colR1:
                    do_reg = st.button("✅ 등록", use_container_width=True, key="do_register_btn")
                with colR2:
                    st.caption("※ 재고관리 표에 **이미 존재하는 상품명만** 등록됩니다. (없는 상품은 제외)")

                if do_reg:
                    sum_df = st.session_state.get("last_sum_df_long")
                    updated, skipped = register_sum_to_inventory(sum_df, target_col=target, add_mode=add_mode)
                    st.session_state["show_register_panel"] = False

                    if skipped:
                        st.warning("등록 제외(재고관리 상품명 없음): " + ", ".join(sorted(set(skipped))))
                    st.success(f"{target}에 등록 완료! (반영 행: {updated})")
                    st.info("📦 사이드바의 '재고관리'로 이동하면 확인할 수 있어요.")

            # PIL 없으면 여러 페이지 합치기 불가 안내
            if Image is None and len(sum_imgs) > 1:
                st.warning("⚠️ Pillow(PIL)가 없어 제품별합계 스크린샷은 1페이지만 PNG로 저장됩니다. 전체를 1장으로 합치려면 Pillow 설치가 필요합니다.")

        except Exception as e:
            st.error(f"제품별 합계 PDF/PNG 생성 실패: {e} (fonts/NanumGothic.ttf 또는 pymupdf 확인)")

    else:
        st.caption("💡 PDF가 스캔본(이미지)이라 텍스트 추출이 안 되면 OCR이 필요합니다.")




# ----- Page Router -----
if st.session_state.get("page") == "inventory":
    render_inventory_page()
else:
    render_pdf_page()
