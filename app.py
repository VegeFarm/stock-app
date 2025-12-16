import io
import os
import re
import math
from datetime import datetime, timezone, timedelta
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

    # ✅ 화면은 "위→아래" 순서로 보이도록 세로우선 배치
    df_wide = to_3_per_row(df_long, 3)

    st.subheader("🧾 제품별 합계")
    st.dataframe(df_wide, use_container_width=True, hide_index=True)

    # ✅ 버튼 2개를 "옆에" 배치: PDF / 스크린샷(PNG 1장)
    try:
        pdf_bytes = make_pdf_bytes(df_wide, "제품별 합계")

        # PDF -> PNG 페이지 렌더 -> 1장으로 합치기
        sum_imgs = render_pdf_pages_to_images(pdf_bytes, zoom=3.0)
        sum_png_one = merge_png_pages_to_one(sum_imgs)

        c1, c2 = st.columns(2)
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

        # PIL 없으면 여러 페이지 합치기 불가 안내
        if Image is None and len(sum_imgs) > 1:
            st.warning("⚠️ Pillow(PIL)가 없어 제품별합계 스크린샷은 1페이지만 PNG로 저장됩니다. 전체를 1장으로 합치려면 Pillow 설치가 필요합니다.")

    except Exception as e:
        st.error(f"제품별 합계 PDF/PNG 생성 실패: {e} (fonts/NanumGothic.ttf 또는 pymupdf 확인)")

else:
    st.caption("💡 PDF가 스캔본(이미지)이라 텍스트 추출이 안 되면 OCR이 필요합니다.")

