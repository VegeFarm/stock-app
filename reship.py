from __future__ import annotations

import io
import re
from typing import Dict, Iterable, List, Optional

from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor


# 재배송 상품명은 사용자가 입력한 표현을 그대로 유지합니다.
# 축약어 치환, 기존 상품명 매칭, 유사도 보정, k→kg 등의 규격 보정을 하지 않습니다.
# 단, 상품 뒤의 수량 표현은 상품 범위에 포함하여 배송메모로 빠지지 않게 합니다.

_PHONE_RE = re.compile(r"(?<!\d)(01[016789])[-.\s]?(\d{3,4})[-.\s]?(\d{4})(?!\d)")

# 상품명 + 규격. 예: 방토3팩, 바질500g, 와일드1k, 고수1단
_PRODUCT_RE = re.compile(
    r"(?P<name>[A-Za-z가-힣][A-Za-z가-힣·ㆍ_-]{0,30})\s*"
    r"(?P<spec>\d+(?:\.\d+)?\s*(?:kg|KG|Kg|k|K|키로|킬로|g|G|그램|팩|개|통|단|봉|박스))"
)

# 상품 규격 뒤에 별도로 붙는 주문 수량.
# 예: 통로메인2k 2개 / 바질500g 3개 / 엔다이브1kg 2봉 / 와일드500g x2
# 이 부분은 배송메모가 아니라 바로 앞 상품에 묶어서 처리합니다.
_TRAILING_QTY_RE = re.compile(
    r"\s*(?:"
    r"(?P<num>\d+)\s*(?P<unit>개|팩|단|통|봉|박스|세트|망|ea)"
    r"|(?P<mult>[xX×*])\s*(?P<multnum>\d+)"
    r"|(?P<korean>한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?P<kunit>개|팩|단|통|봉|박스|세트|망)"
    r")",
    re.IGNORECASE,
)

_KOREAN_QTY_NUMBERS = {
    "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}

_ADDRESS_START_RE = re.compile(
    r"(?:서울(?:특별시)?|부산(?:광역시)?|대구(?:광역시)?|인천(?:광역시)?|광주(?:광역시)?|"
    r"대전(?:광역시)?|울산(?:광역시)?|세종(?:특별자치시)?|경기(?:도)?|강원(?:특별자치도)?|"
    r"충청북도|충북|충청남도|충남|전북(?:특별자치도)?|전라북도|전라남도|전남|"
    r"경상북도|경북|경상남도|경남|제주(?:특별자치도)?)"
)
_ADDRESS_ROAD_RE = re.compile(r"[가-힣0-9·ㆍ-]+(?:대로|로|길)\s*\d+(?:-\d+)?")
_FALLBACK_ADDRESS_RE = re.compile(
    r"[가-힣0-9·ㆍ-]+(?:시|군|구)\s+[가-힣0-9·ㆍ-]+(?:대로|로|길)\s*\d+(?:-\d+)?(?:\s+[^,;\n]+)?"
)

_LABEL_RE = re.compile(
    r"(?:배송\s*정보\s*표|배송지\s*정보|배송\s*정보|수취인명|수령인명|받는\s*사람|"
    r"연락처\s*1|연락처\s*2|연락처|전화번호|배송지\s*주소|배송지|주소|"
    r"상품명|상품\s*목록|상품|배송메모|배송\s*메모|배송메세지|배송메시지|요청사항)\s*[:：-]?",
    re.IGNORECASE,
)
_DATE_NOISE_RE = re.compile(
    r"(?:월|화|수|목|금|토|일)\s*요일|(?:오늘|내일|모레)|\d{1,2}[./-]\d{1,2}(?:[./-]\d{1,2})?|재배송",
    re.IGNORECASE,
)
_MEMO_HINT_RE = re.compile(
    r"문\s*앞|문앞|경비실|벨|전화|연락|부재|공동현관|공동\s*현관|비밀번호|비번|출입|호출|"
    r"맡겨|놓아|놔|두고|두어|말아|주세요|부탁|수령실|택배함|직접|후문|정문|현관|"
    r"배송메모|배송\s*메모|배송메세지|배송메시지|요청사항",
    re.IGNORECASE,
)

# 배송메모가 문장이 아니라 출입코드만 있는 경우도 허용합니다.
# 예: #123#24 / 1234# / *1234* / #2580 / 1234*
# 단, '보꼬네485'의 485처럼 이름에 붙은 일반 숫자는 메모로 보지 않습니다.
_ACCESS_CODE_RE = re.compile(
    r"(?<![A-Za-z가-힣0-9])(?:"
    r"[#*]\s*\d+(?:\s*[#*]\s*\d+)*(?:\s*[#*])?"
    r"|\d+\s*[#*](?:\s*\d+)*(?:\s*[#*])?"
    r")(?![A-Za-z가-힣0-9])"
)

_MEMO_LABEL_RE = re.compile(
    r"(?:배송메모|배송\s*메모|배송메세지|배송메시지|요청사항)\s*[:：-]?\s*(.+)",
    re.IGNORECASE,
)

# 여러 줄 주소가 들어올 때 주소를 끝내는 명확한 필드 라벨입니다.
# 예: 도로명 주소 다음 줄의 "1층 매장"은 주소에 붙이고,
# "연락처1", "상품", "배송메모" 등이 나오면 주소를 종료합니다.
_ADDRESS_FIELD_BOUNDARY_RE = re.compile(
    r"^(?:수취인명|수령인명|받는\s*사람|연락처\s*1|연락처\s*2|연락처|전화번호|"
    r"상품명|상품\s*목록|상품|배송메모|배송\s*메모|배송메세지|배송메시지|요청사항)\b",
    re.IGNORECASE,
)


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


def _normalize_spaces(s: str) -> str:
    s = str(s or "").replace("\u00a0", " ").replace("\t", " ")
    s = re.sub(r"[ ]+", " ", s)
    s = re.sub(r"\n[ ]+", "\n", s)
    return s.strip()


def _normalize_phone(m: re.Match) -> str:
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _iter_product_matches(text: str):
    """상품 규격과 바로 뒤의 별도 주문수량까지 한 묶음으로 반환합니다."""
    for m in _PRODUCT_RE.finditer(text):
        # 앞선 상품의 trailing quantity 안에서 다시 상품으로 오인식되는 경우 방지
        start, end = m.span()
        qty_match = _TRAILING_QTY_RE.match(text, end)
        full_end = qty_match.end() if qty_match else end
        yield m, qty_match, (start, full_end)


def _extract_products(text: str, mapping_rules: Iterable[Dict]) -> tuple[List[str], str]:
    """상품 문구를 입력된 그대로 추출합니다.

    mapping_rules 인자는 기존 호출부 호환성을 위해 유지하지만 상품명 변환에는 사용하지 않습니다.
    예:
      - 방토3팩 -> 방토3팩
      - 통로2k -> 통로2k
      - 통로메인2k 2개 -> 통로메인2k 2개
      - 와일드500g x2 -> 와일드500g x2
      - 바질1kg 두개 -> 바질1kg 두개
    """
    products: List[str] = []
    spans = []
    last_end = -1

    for _m, _qty_match, full_span in _iter_product_matches(text):
        start, full_end = full_span
        if start < last_end:
            continue

        # 상품명/규격/수량 표현을 변환하지 않고 입력된 문자열 그대로 사용합니다.
        product = text[start:full_end].strip()
        if product and product not in products:
            products.append(product)

        spans.append((start, full_end))
        last_end = full_end

    if not spans:
        return products, text

    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return products, "".join(chars)


def _extract_labeled_name(text: str) -> str:
    m = re.search(
        r"(?:수취인명|수령인명|받는\s*사람)\s*[:：-]?\s*([가-힣A-Za-z][가-힣A-Za-z0-9 .·ㆍ_-]{1,39}?)"
        r"(?=\s*(?:연락처|전화번호|배송지|주소|상품|배송메모|배송\s*메모|$))",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip(" -:/")


def _extract_address(text: str) -> tuple[str, str]:
    """주소와 주소를 제거한 텍스트를 반환합니다.

    여러 줄 주소를 지원합니다. 도로명 주소가 시작된 뒤 다음 줄이
    상세주소처럼 보이면 주소에 이어 붙이고, 연락처/상품/배송메모 등
    다음 명확한 필드가 시작되면 주소를 종료합니다.
    """
    work = text

    def _clean_address_label(value: str) -> str:
        # 주소 결과에는 '배송지', '주소', '배송지 주소' 같은 라벨을 남기지 않습니다.
        value = re.sub(r"^\s*(?:배송지\s*주소|배송지|주소)\s*[:：-]?\s*", "", value, flags=re.IGNORECASE)

        # 네이버 등에서 복사할 때 주소 맨 앞에 붙는 우편번호는 재배송 주소에 넣지 않습니다.
        # 예: (38078) 경상북도 ... -> 경상북도 ...
        #     우편번호 38078 경상북도 ... -> 경상북도 ...
        value = re.sub(r"^\s*우편번호\s*[:：-]?\s*\d{5}\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"^\s*[\(（\[]\s*\d{5}\s*[\)）\]]\s*", "", value)

        return re.sub(r"\s+", " ", value).strip(" ,;/")

    def _is_address_boundary(line: str) -> bool:
        stripped = str(line or "").strip()
        if not stripped:
            return True
        if _ADDRESS_FIELD_BOUNDARY_RE.search(stripped):
            return True
        if "<<PRODUCT>>" in stripped:
            return True
        if _MEMO_HINT_RE.search(stripped):
            return True
        if re.search(r"(?:월|화|수|목|금|토|일)요일", stripped, re.IGNORECASE):
            return True
        if re.search(r"재배송", stripped, re.IGNORECASE):
            return True
        return False

    # 1) 여러 줄 입력: 도로명 주소가 있는 줄부터 상세주소 줄을 이어 붙입니다.
    if "\n" in work:
        lines = work.splitlines()
        for idx, line in enumerate(lines):
            if _ADDRESS_START_RE.search(line) and _ADDRESS_ROAD_RE.search(line):
                collected = [line]
                consumed_indexes = [idx]

                # 다음 줄이 상세주소라면 계속 붙입니다.
                # 예: '1층 닭 한스포', '101동 1204호', '지하1층 매장'
                j = idx + 1
                while j < len(lines):
                    nxt = lines[j]
                    if _is_address_boundary(nxt):
                        break
                    collected.append(nxt)
                    consumed_indexes.append(j)
                    j += 1

                candidate = _clean_address_label(" ".join(collected))
                if _ADDRESS_ROAD_RE.search(candidate):
                    for k in consumed_indexes:
                        lines[k] = ""
                    return candidate, "\n".join(lines)

    # 2) 한 줄 입력: 기존처럼 주소 시작점부터 상품/메모/재배송 문구 전까지 사용합니다.
    starts = list(_ADDRESS_START_RE.finditer(work))
    if starts:
        start = starts[0].start()
        tail = work[start:]
        boundaries = []
        for pat in (
            re.compile(r"<<PRODUCT>>"),
            _MEMO_HINT_RE,
            re.compile(r"(?:월|화|수|목|금|토|일)요일", re.IGNORECASE),
            re.compile(r"재배송", re.IGNORECASE),
        ):
            mm = pat.search(tail)
            if mm and mm.start() > 0:
                boundaries.append(mm.start())
        end_rel = min(boundaries) if boundaries else len(tail)
        candidate = _clean_address_label(tail[:end_rel])
        if _ADDRESS_ROAD_RE.search(candidate):
            end = start + end_rel
            remaining = work[:start] + " " + work[end:]
            return candidate, remaining

    # 3) 서울특별시 등이 생략되고 '마포구 신촌로 260-1 ...' 형태인 경우
    mm = _FALLBACK_ADDRESS_RE.search(work)
    if mm:
        candidate = _clean_address_label(mm.group(0))
        remaining = work[: mm.start()] + " " + work[mm.end() :]
        return candidate, remaining

    # 4) 줄 단위 최종 보조: 로/길/대로 + 번지가 있는 줄
    lines = work.splitlines()
    for idx, line in enumerate(lines):
        if _ADDRESS_ROAD_RE.search(line):
            collected = [line]
            consumed_indexes = [idx]
            j = idx + 1
            while j < len(lines):
                nxt = lines[j]
                if _is_address_boundary(nxt):
                    break
                collected.append(nxt)
                consumed_indexes.append(j)
                j += 1
            cleaned = _clean_address_label(" ".join(collected))
            for k in consumed_indexes:
                lines[k] = ""
            return cleaned, "\n".join(lines)

    return "", work

def _strip_quotes(text: str) -> str:
    # 큰따옴표/작은따옴표 자체만 제거하고 안의 내용은 유지합니다.
    # 예: "#123#24" -> #123#24 / "문앞에 놔주세요" -> 문앞에 놔주세요
    return re.sub(r'["“”\'‘’]+', "", str(text or ""))


def _cleanup_leftover(text: str) -> str:
    s = _LABEL_RE.sub(" ", text)
    s = _DATE_NOISE_RE.sub(" ", s)
    s = s.replace("<<PRODUCT>>", " ")
    s = _strip_quotes(s)
    s = re.sub(r"[,;/|]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -:/")


def _extract_explicit_memo(text: str) -> str:
    """라벨이 붙은 배송메모는 내용 종류와 관계없이 우선 인정합니다.

    따옴표는 문자만 제거하고 내부 내용은 보존합니다.
    """
    for raw_line in str(text or "").splitlines():
        m = _MEMO_LABEL_RE.search(raw_line)
        if not m:
            continue
        value = _strip_quotes(m.group(1)).strip()
        # 라벨 뒤에 재배송 요일만 들어온 잡음은 메모로 보지 않습니다.
        value = _DATE_NOISE_RE.sub(" ", value)
        value = re.sub(r"\s+", " ", value).strip(" -:/")
        if value:
            return value[:100]
    return ""


def _extract_generic_name(leftover: str) -> tuple[str, str]:
    # 이름/상호명 뒤에 붙은 숫자까지 한 덩어리로 유지합니다.
    # 예: 보꼬네485 / 카페24 / 제일상회2호점
    tokens = re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9·ㆍ_-]{1,39}", leftover)
    banned = {
        "배송정보", "배송지", "정보", "표", "연락처", "전화번호", "상품", "목록", "주소",
        "배송", "메모", "요청사항", "문앞", "경비실", "공동현관", "비밀번호", "비번",
        "전화", "연락", "부재", "재배송",
    }
    for token in tokens:
        compact = _compact(token)
        if compact in banned:
            continue
        if _MEMO_HINT_RE.search(token):
            continue
        # 행정구역/도로명처럼 보이는 토큰 제외
        if re.search(r"(?:특별시|광역시|시|군|구|동|읍|면|리|로|길|대로)$", token):
            continue
        m = re.search(re.escape(token), leftover)
        if m:
            rest = leftover[: m.start()] + " " + leftover[m.end() :]
        else:
            rest = leftover
        return token.strip(), rest
    return "", leftover


def _extract_safe_memo(leftover: str, explicit_memo: str = "") -> str:
    """실제 배송요청으로 판단되는 내용만 배송메모로 반환합니다.

    - '남은 글자 = 배송메모' 방식은 사용하지 않습니다.
    - 명시적인 배송메모 라벨이 있으면 우선 사용합니다.
    - 라벨이 없으면 배송 요청 키워드 또는 #/*가 포함된 출입코드가 있어야 합니다.
    - 따라서 '배송정보 표', 이름 일부 숫자(보꼬네485의 485) 같은 잡음은 버립니다.
    """
    if explicit_memo:
        return explicit_memo[:100]

    s = _cleanup_leftover(leftover)
    if not s:
        return ""

    if _MEMO_HINT_RE.search(s) or _ACCESS_CODE_RE.search(s):
        return s[:100]

    return ""


def _split_blocks(raw_text: str) -> List[str]:
    text = _normalize_spaces(raw_text)
    if not text:
        return []

    blocks = [b.strip() for b in re.split(r"\n\s*\n+", text) if b.strip()]
    out: List[str] = []
    for block in blocks:
        phones = list(_PHONE_RE.finditer(block))
        if len(phones) <= 1:
            out.append(block)
            continue

        # 한 블록 안에 여러 수취인이 붙어 있는 경우, 전화번호가 있는 줄을 경계로 나눕니다.
        lines = block.splitlines()
        phone_line_indexes = [i for i, line in enumerate(lines) if _PHONE_RE.search(line)]
        if len(phone_line_indexes) <= 1:
            out.append(block)
            continue

        # 전화번호가 별도 줄에 있는 경우 바로 앞의 이름 줄까지 같은 수취인 블록으로 묶습니다.
        starts = []
        for i in phone_line_indexes:
            s = max(0, i - 1)
            if not starts or s > starts[-1]:
                starts.append(s)
        for pos, start in enumerate(starts):
            end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
            chunk = "\n".join(lines[start:end]).strip()
            if chunk:
                out.append(chunk)

    return out


def parse_reship_text(raw_text: str, mapping_rules: Optional[Iterable[Dict]] = None) -> List[Dict[str, str]]:
    """자유형 재배송 메모를 항목별로 분리합니다. 상품 문구는 입력값 그대로 유지합니다."""
    rules = list(mapping_rules or [])
    results: List[Dict[str, str]] = []

    for block in _split_blocks(raw_text):
        original = block
        labeled_name = _extract_labeled_name(original)
        explicit_memo = _extract_explicit_memo(original)

        # 전화번호
        phone_match = _PHONE_RE.search(block)
        phone = _normalize_phone(phone_match) if phone_match else ""
        if phone_match:
            block = block[: phone_match.start()] + " " + block[phone_match.end() :]

        # 상품
        products, block = _extract_products(block, rules)
        # 주소 추출의 경계를 잡기 위해 상품 자리에 마커를 넣은 원문도 별도로 사용
        marked = original
        # 주소/메모 분리용 원문에서도 상품 뒤 수량까지 함께 제거합니다.
        # 그래야 '통로메인2k 2개'의 '2개'가 배송메모로 남지 않습니다.
        product_spans = [full_span for _, _, full_span in _iter_product_matches(marked)]
        for start, end in reversed(product_spans):
            marked = marked[:start] + " <<PRODUCT>> " + marked[end:]
        marked = _PHONE_RE.sub(" ", marked)

        address, marked_remaining = _extract_address(marked)

        # 주소를 일반 block에서도 제거하여 나머지 문장을 메모/이름 후보로 사용
        remaining = marked_remaining
        remaining = _cleanup_leftover(remaining)

        name = labeled_name
        if name:
            # 남은 문자열에서 이름 제거
            remaining = re.sub(re.escape(name), " ", remaining, count=1)
            remaining = re.sub(r"\s+", " ", remaining).strip()
        else:
            name, remaining = _extract_generic_name(remaining)

        memo = _extract_safe_memo(remaining, explicit_memo=explicit_memo)

        results.append(
            {
                "수취인": name,
                "연락처": phone,
                "주소": address,
                "배송메모": memo,
                "상품목록": ", ".join(products),
            }
        )

    return results


def _set_run_font(run, font_name: str = "맑은 고딕", font_size: int = 14) -> None:
    run.font.name = font_name
    run.font.size = Pt(font_size)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), font_name)
    rfonts.set(qn("w:hAnsi"), font_name)
    rfonts.set(qn("w:eastAsia"), font_name)


def _prefix_width_pt(prefix: str, font_size: int = 14) -> float:
    # 14pt 한글은 대략 1em, 영문/숫자는 약 0.55em으로 계산합니다.
    units = 0.0
    for ch in prefix:
        if "가" <= ch <= "힣":
            units += 1.0
        elif ch.isspace():
            units += 0.35
        else:
            units += 0.58
    return max(35.0, units * font_size)


def _product_width_pt(text: str, font_size: int = 14) -> float:
    units = 0.0
    for ch in text:
        if "가" <= ch <= "힣":
            units += 1.0
        elif ch.isspace():
            units += 0.35
        else:
            units += 0.58
    return units * font_size


def _wrap_products(products_text: str, max_width_pt: float, font_size: int = 14) -> List[str]:
    items = [x.strip() for x in str(products_text or "").split(",") if x.strip()]
    if not items:
        return [""]

    lines: List[str] = []
    current = ""
    for idx, item in enumerate(items):
        token = item + ("," if idx < len(items) - 1 else "")
        candidate = token if not current else f"{current} {token}"
        if current and _product_width_pt(candidate, font_size) > max_width_pt:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def build_reship_docx(entries: List[Dict[str, str]]) -> bytes:
    """
    재배송 Word 문서 생성.
    - 여백: 좁게(12.7mm)
    - 단: 2단
    - 글자: 14pt
    - 첫 줄: 이름 - 상품...
    - 다음 줄: 상품 시작 위치에 맞춰 들여쓰기
    - 수취인 사이: 빈 줄 1줄
    - 송장수량이 2 이상이면 이름 왼쪽에 (2), (3) 형태로 표시
      (상품 문구는 한 번만 표시하며 별도의 수량 설명문은 넣지 않음)
    """
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Mm(12.7)
    section.bottom_margin = Mm(12.7)
    section.left_margin = Mm(12.7)
    section.right_margin = Mm(12.7)

    # Word의 2단 설정
    sect_pr = section._sectPr
    cols = sect_pr.xpath("./w:cols")
    cols_el = cols[0] if cols else OxmlElement("w:cols")
    cols_el.set(qn("w:num"), "2")
    cols_el.set(qn("w:space"), "720")  # 0.5 inch
    if not cols:
        sect_pr.append(cols_el)

    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "맑은 고딕"
    normal.font.size = Pt(14)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")

    # A4 210mm - 좌우 25.4mm - 단 사이 12.7mm => 한 단 약 85.95mm
    column_width_pt = (85.95 / 25.4) * 72.0

    clean_entries = [e for e in entries if any(str(e.get(k, "") or "").strip() for k in ("수취인", "상품목록"))]
    for idx, entry in enumerate(clean_entries):
        name = str(entry.get("수취인", "") or "").strip()
        products = str(entry.get("상품목록", "") or "").strip()
        try:
            shipment_count = max(1, int(float(str(entry.get("송장수량", 1) or 1).strip())))
        except Exception:
            shipment_count = 1

        prefix = f"{name} - " if name else ""
        # 송장수량이 2 이상이면 첨자가 아니라 이름 왼쪽에 (2), (3) 형태로 표시합니다.
        # 수량 표시는 본문보다 조금 크게 보여도 줄 위로 겹치지 않도록 일반 글자 위치를 사용합니다.
        count_prefix = f"({shipment_count}) " if shipment_count > 1 else ""
        if count_prefix:
            # (N)도 본문과 같은 14pt이므로 같은 크기를 기준으로 들여쓰기 폭을 계산합니다.
            indent_pt = min(
                _product_width_pt(count_prefix, 14) + _prefix_width_pt(prefix, 14),
                column_width_pt * 0.55,
            )
        else:
            indent_pt = min(_prefix_width_pt(prefix, 14), column_width_pt * 0.55)
        product_area_pt = max(60.0, column_width_pt - indent_pt - 3.0)
        product_lines = _wrap_products(products, product_area_pt, 14)

        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.left_indent = Pt(indent_pt)
        pf.first_line_indent = Pt(-indent_pt)
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.line_spacing = 1.0
        pf.keep_together = True

        if shipment_count > 1:
            count_run = p.add_run(f"({shipment_count})")
            _set_run_font(count_run, font_size=14)
            count_run.font.bold = False
            count_run.font.color.rgb = RGBColor(0xD9, 0x9A, 0x9A)
            spacer_run = p.add_run(" ")
            _set_run_font(spacer_run)

        r = p.add_run(prefix)
        _set_run_font(r)
        for line_idx, line in enumerate(product_lines):
            if line_idx > 0:
                br = p.add_run()
                br.add_break()
                _set_run_font(br)
            rr = p.add_run(line)
            _set_run_font(rr)

        # 다른 수취인 재배송건은 한 칸(빈 줄 1줄) 띄움
        if idx < len(clean_entries) - 1:
            blank = doc.add_paragraph()
            blank.paragraph_format.space_before = Pt(0)
            blank.paragraph_format.space_after = Pt(0)
            blank.paragraph_format.line_spacing = 1.0
            rb = blank.add_run("")
            _set_run_font(rb)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
