import io
import os
import re
from collections import defaultdict

import pandas as pd
import streamlit as st

# -------------------- PDF libs --------------------
try:
    import pdfplumber  # pip install pdfplumber
except Exception:
    pdfplumber = None

try:
    from pypdf import PdfReader  # pip install pypdf
except Exception:
    try:
        from PyPDF2 import PdfReader  # pip install PyPDF2 (fallback)
    except Exception:
        PdfReader = None


COUNT_UNITS = ["개", "통", "팩", "봉"]  # 필요하면 추가 가능


# -------------------- Rules (customization) --------------------
DEFAULT_RULES_TEXT = """# TYPE,상품명,값
# PACK,상품명,팩_기준_g(=1팩이 몇 g인지)
# BOX,상품명,박스_기준_kg(기본 2kg면 2 입력)

PACK,건대추,500
PACK,양송이,500

BOX,적겨자,2
BOX,적근대,2
"""

RULES_FILE = "rules.txt"  # (선택) 레포에 rules.txt를 만들어두면 기본값으로 자동 로드


def norm_type(t: str) -> str:
    t = (t or "").strip()
    if t in ["팩", "PACK", "pack", "Pack"]:
        return "PACK"
    if t in ["박스", "BOX", "box", "Box"]:
        return "BOX"
    return t.upper().strip()


def parse_pack_size_g(val: str) -> float:
    """PACK 값: 500 / 500g / 0.5kg 모두 허용 -> g로 변환"""
    v = (val or "").strip().lower().replace(" ", "")
    if v.endswith("kg"):
        return float(v[:-2]) * 1000.0
    if v.endswith("g"):
        return float(v[:-1])
    return float(v)  # 숫자만이면 g로 간주


def load_rules_text() -> str:
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return DEFAULT_RULES_TEXT


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

        typ = norm_type(parts[0])   # <-- 핵심: '팩'도 'PACK'으로 바꿈
        name = parts[1].strip()
        val_raw = parts[2].strip()

        try:
            if typ == "PACK":
                size_g = parse_pack_size_g(val_raw)  # 500 / 500g / 0.5kg OK
                if size_g > 0:
                    pack_rules[name] = {"size_g": size_g}

            elif typ == "BOX":
                # BOX 값은 kg 기준(2 / 2kg / 2000g 도 허용)
                v = val_raw.strip().lower().replace(" ", "")
                if v.endswith("g"):
                    size_kg = float(v[:-1]) / 1000.0
                elif v.endswith("kg"):
                    size_kg = float(v[:-2])
                else:
                    size_kg = float(v)

                if size_kg > 0:
                    box_rules[name] = {"size_kg": size_kg}

        except Exception:
            continue

    return pack_rules, box_rules



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
    """
    반환: (제품명, 구분텍스트, 수량)
    - "고수 100g 2" 같은 한 줄 형태
    - "고수 100g" 다음 줄 "2" 형태 모두 처리
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

        # 끝에 수량이 붙은 줄: "... 10"
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
    """
    spec에 복합표현이 있어도 최대한 뽑아냄.
    예) "1팩 500g" -> 팩=1, g=500
    예) "2단" -> 단=2
    """
    if not spec:
        return None

    s = spec.replace(",", "").replace(" ", "")
    s = s.replace("㎏", "kg").replace("ＫＧ", "kg").replace("KG", "kg")
    s = s.lower()

    out = {
        "grams_per_unit": None,      # float
        "bunch_per_unit": None,      # int
        "counts_per_unit": {},       # {unit: int}  e.g. {"팩": 1}
    }

    # weight: 가장 첫 번째 kg/g 패턴 1개만 잡아도 충분(팩+g 같이 있을 때도 OK)
    mw = re.search(r"(\d+(?:\.\d+)?)(kg|g)", s)
    if mw:
        num = float(mw.group(1))
        unit = mw.group(2)
        out["grams_per_unit"] = num * 1000.0 if unit == "kg" else num

    # bunch
    mb = re.search(r"(\d+)단", s)
    if mb:
        out["bunch_per_unit"] = int(mb.group(1))

    # count units
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
    s = f"{x:.{max_dec}f}"
    s = s.rstrip("0").rstrip(".")
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


def format_total_custom(product: str, rec, pack_rules, box_rules) -> str:
    parts = []

    # 단은 항상 표기(필요 없으면 지워도 됨)
    if rec["bunch"]:
        parts.append(f'{rec["bunch"]}단')

    grams = rec["grams"]
    counts = dict(rec["counts"])

    # 우선순위: BOX 규칙 > PACK 규칙 > 기본(kg/g + 개/통/팩/봉)
    if product in box_rules and grams > 0:
        box_size_kg = float(box_rules[product]["size_kg"])
        total_kg = grams / 1000.0
        boxes = total_kg / box_size_kg
        parts.append(f"{fmt_num(boxes, max_dec=2)}박스")
        # 박스표현이면 무게/팩표현은 숨김(원하면 여기서 무게도 같이 표시 가능)
        # counts(팩/개 등)는 보통 없지만, 있으면 같이 표시
        for u in ["개", "통", "팩", "봉"]:
            if counts.get(u, 0):
                parts.append(f"{counts[u]}{u}")
        return " ".join(parts).strip() if parts else "0"

    pack_shown = False

    # 1) spec 자체에 "팩"이 있었으면 그걸 그대로 존중
    if counts.get("팩", 0) > 0:
        parts.append(f'{counts["팩"]}팩')
        pack_shown = True
        counts.pop("팩", None)  # 중복표기 방지

    # 2) spec에 팩이 없는데, 네가 PACK 규칙 지정한 상품이면 g를 팩으로 변환
    elif product in pack_rules and grams > 0:
        size_g = float(pack_rules[product]["size_g"])
        if size_g > 0 and (int(round(grams)) % int(round(size_g)) == 0):
            packs = int(round(grams / size_g))
            parts.append(f"{packs}팩")
            pack_shown = True
        # 딱 나눠떨어지지 않으면 기본 무게로 표시(원하면 여기서 소수팩으로도 가능)

    # 무게 표기: 팩으로 보여주기로 했으면 무게는 숨김(원하면 같이 표기 가능)
    if not pack_shown:
        w = format_weight(grams)
        if w:
            parts.append(w)

    # 나머지 카운트 단위(개/통/봉/팩 등)
    for u in ["개", "통", "봉"]:  # 팩은 위에서 처리
        if counts.get(u, 0):
            parts.append(f"{counts[u]}{u}")

    # 파싱 못한 구분이 있으면 참고용으로 붙임(싫으면 아래 블록 삭제)
    for spec, q in rec["unknown"].items():
        if spec:
            parts.append(f"{spec}×{q}")

    return " ".join(parts).strip() if parts else "0"


# -------------------- Streamlit UI --------------------
st.set_page_config(page_title="제품별 수량 합산", layout="wide")
st.title("제품별 수량 합산(PDF 업로드)")

with st.sidebar:
    st.subheader("표현 규칙(커스터마이징)")
    rules_text = st.text_area(
        "PACK/BOX 규칙 입력",
        value=load_rules_text(),
        height=220,
        help="예: PACK,건대추,500  /  BOX,적겨자,2",
    )
    pack_rules, box_rules = parse_rules(rules_text)

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
            "합계": format_total_custom(product, agg[product], pack_rules, box_rules),
        })

    df = pd.DataFrame(rows)
    st.subheader("제품별 합계")
    st.dataframe(df, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("CSV 다운로드", data=csv, file_name="제품별_합계.csv", mime="text/csv")
else:
    st.caption("※ PDF가 스캔본(이미지)이라 텍스트 추출이 안 되면 OCR 처리가 필요합니다.")

