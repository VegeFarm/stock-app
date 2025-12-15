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


# -------------------- PDF libs --------------------
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


# -------------------- Rules helpers --------------------
def norm_type(t: str) -> str:
    t = (t or "").strip()
    if t in ["팩", "PACK", "pack", "Pack"]:
        return "PACK"
    if t in ["박스", "BOX", "box", "Box"]:
        return "BOX"
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
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass

    # 기본값(원하면 여기에 자주 쓰는 리스트 더 넣어도 됨)
    return """# TYPE,상품명,값
# PACK,상품명,팩_기준_g(=1팩이 몇 g인지)  ex) 500 / 500g / 0.5kg
# BOX,상품명,박스_기준_kg(예: 2면 총중량/2=박스) ex) 2 / 2kg / 2000g

팩,건대추,500
팩,양송이,500

박스,적겨자,2
박스,적근대,2
"""


def save_rules_text(text: str) -> None:
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        f.write(text or "")


def parse_rules(text: str):
    pack_rules = {}  # {상품명: {"size_g": float}}
    box_rules = {}   # {상품명: {"size_kg": float}}

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
        except Exception:
            continue

    return pack_rules, box_rules


def upsert_rule(text: str, typ: str, name: str, val: str) -> str:
    typ = norm_type(typ)
    name = (name or "").strip()
    val = (val or "").strip()
    if not typ or not name or not val:
        return text

    lines = (text or "").splitlines()
    out = []
    replaced = False

    for ln in lines:
        if ln.strip().startswith("#") or not ln.strip():
            out.append(ln)
            continue

        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 2 and norm_type(parts[0]) == typ and parts[1] == name:
            out.append(f"{typ},{name},{val}")
            replaced = True
        else:
            out.append(ln)

    if not replaced:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{typ},{name},{val}")

    return "\n".join(out)


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
        raise RuntimeError("pdfplumber 또는 pypdf(PyPDF2)가 필요합니다. (pip install pdfplumber pypdf)")

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
    if grams <= 0:
        return None
    g = int(round(grams))
    kg = g // 1000
    rem = g % 1000
    if kg > 0 and rem == 0:
        return f"{kg}kg"
    if kg > 0 and rem > 0:
        return f"{kg}kg {rem}g"
    return f"{g}g"


def format_total_custom(product: str, rec, pack_rules, box_rules, allow_decimal_pack: bool, allow_decimal_box: bool) -> str:
    parts = []

    if rec["bunch"]:
        parts.append(f'{rec["bunch"]}단')

    grams = rec["grams"]
    counts = dict(rec["counts"])

    # BOX 우선
    if product in box_rules and grams > 0:
        box_size_kg = float(box_rules[product]["size_kg"])
        total_kg = grams / 1000.0
        boxes = total_kg / box_size_kg

        if allow_decimal_box:
            parts.append(f"{fmt_num(boxes, 2)}박스")
        else:
            if abs(boxes - round(boxes)) < 1e-9:
                parts.append(f"{int(round(boxes))}박스")
            else:
                w = format_weight(grams)
                if w:
                    parts.append(w)

        for u in ["개", "통", "팩", "봉"]:
            if counts.get(u, 0):
                parts.append(f"{counts[u]}{u}")
        return " ".join(parts).strip() if parts else "0"

    # PACK 처리
    pack_shown = False

    # spec 자체에 팩이 있으면 그걸 우선
    if counts.get("팩", 0) > 0:
        parts.append(f'{counts["팩"]}팩')
        pack_shown = True
        counts.pop("팩", None)

    # rules로 g -> 팩 변환
    elif product in pack_rules and grams > 0:
        size_g = float(pack_rules[product]["size_g"])
        packs = grams / size_g
        if allow_decimal_pack:
            parts.append(f"{fmt_num(packs, 2)}팩")
            pack_shown = True
        else:
            if abs(packs - round(packs)) < 1e-9:
                parts.append(f"{int(round(packs))}팩")
                pack_shown = True

    if not pack_shown:
        w = format_weight(grams)
        if w:
            parts.append(w)

    for u in ["개", "통", "봉"]:
        if counts.get(u, 0):
            parts.append(f"{counts[u]}{u}")

    return " ".join(parts).strip() if parts else "0"


def to_3_per_row(df: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    out = []
    for i in range(0, len(df), n):
        chunk = df.iloc[i:i+n].reset_index(drop=True)
        row = {}
        for j in range(n):
            if j < len(chunk):
                row[f"제품명{j+1}"] = chunk.loc[j, "제품명"]
                row[f"합계{j+1}"] = chunk.loc[j, "합계"]
            else:
                row[f"제품명{j+1}"] = ""
                row[f"합계{j+1}"] = ""
        out.append(row)
    return pd.DataFrame(out)


def make_pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=18, rightMargin=18, topMargin=18, bottomMargin=18
    )

    styles = getSampleStyleSheet()
    elements = [
        Paragraph(title, styles["Title"]),
        Spacer(1, 12),
    ]

    safe_df = df.fillna("").astype(str)
    data = [list(safe_df.columns)] + safe_df.values.tolist()

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
    ]))

    elements.append(table)
    doc.build(elements)
    return buf.getvalue()



# -------------------- Streamlit UI --------------------
st.set_page_config(page_title="제품별 수량 합산", layout="wide")
st.title("제품별 수량 합산(PDF 업로드)")

if "rules_text" not in st.session_state:
    st.session_state["rules_text"] = load_rules_text()

with st.sidebar:
    st.subheader("표현 규칙(기본값 + 수정 가능)")

    up = st.file_uploader("rules.txt 업로드(선택)", type=["txt"])
    if up is not None:
        st.session_state["rules_text"] = up.getvalue().decode("utf-8", errors="ignore")

    st.text_area("PACK/BOX 규칙", key="rules_text", height=220)

    colA, colB = st.columns(2)
    with colA:
        allow_decimal_pack = st.checkbox("팩 소수 허용", value=False)
    with colB:
        allow_decimal_box = st.checkbox("박스 소수 허용", value=True)

    with st.form("add_rule_form", clear_on_submit=False):
        st.markdown("**규칙 추가/업데이트**")
        r_type = st.selectbox("TYPE", ["팩", "박스", "PACK", "BOX"])
        r_name = st.text_input("상품명(원본 제품명과 동일)", value="")
        r_val = st.text_input("값(PACK=1팩 g, BOX=박스 기준 kg)", value="")
        submitted = st.form_submit_button("추가/업데이트")
        if submitted:
            st.session_state["rules_text"] = upsert_rule(st.session_state["rules_text"], r_type, r_name, r_val)
            st.success("규칙 반영 완료!")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("rules.txt로 저장(로컬용)"):
            try:
                save_rules_text(st.session_state["rules_text"])
                st.success("rules.txt 저장 완료!")
            except Exception as e:
                st.error(f"저장 실패: {e}")

    with col2:
        st.download_button(
            "rules.txt 다운로드",
            data=st.session_state["rules_text"].encode("utf-8"),
            file_name="rules.txt",
            mime="text/plain",
        )

    show_debug = st.checkbox("디버그(원본 파싱 보기)", value=False)

pack_rules, box_rules = parse_rules(st.session_state["rules_text"])

uploaded = st.file_uploader("PDF 업로드", type=["pdf"])

if uploaded:
    file_bytes = uploaded.getvalue()
    lines = extract_lines_from_pdf(file_bytes)
    items = parse_items(lines)
    agg = aggregate(items)

    rows = []
    for product in sorted(agg.keys()):
        rows.append({
            "제품명": product,
            "합계": format_total_custom(
                product, agg[product], pack_rules, box_rules,
                allow_decimal_pack=allow_decimal_pack,
                allow_decimal_box=allow_decimal_box
            ),
        })

    df_long = pd.DataFrame(rows)
    df_wide = to_3_per_row(df_long, 3)

    st.subheader("제품별 합계 (1행에 3개)")
    st.dataframe(df_wide, use_container_width=True, hide_index=True)
    
    pdf_bytes = make_pdf_bytes(df_wide, "제품별 합계 (1행에 3개)")
    st.download_button(
       "PDF 다운로드",
        data=pdf_bytes,
        file_name="제품별_합계.pdf",
        mime="application/pdf",
    )

    if show_debug:
        st.subheader("디버그: 원본 파싱 결과(제품명/구분/수량)")
        st.dataframe(pd.DataFrame(items, columns=["제품명", "구분", "수량"]), use_container_width=True, hide_index=True)

    csv = df_long.to_csv(index=False).encode("utf-8-sig")
    st.download_button("CSV 다운로드(세로형)", data=csv, file_name="제품별_합계.csv", mime="text/csv")

else:
    st.caption("※ PDF가 스캔본(이미지)이라 텍스트 추출이 안 되면 OCR이 필요합니다.")

