from __future__ import annotations

import io
import re
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional

from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt


# 재배송 입력에서 자주 쓰는 축약어만 내부적으로 보정합니다.
# 별도 설정 화면은 만들지 않고, 기존 상품명 매칭 규칙을 우선 사용합니다.
_BUILTIN_ALIASES = {
    "방토": "방울토마토",
    "방울토마토": "방울토마토",
    "와일드루꼴라": "와일드",
    "와일드루콜라": "와일드",
    "와일드": "와일드",
    "루꼴라": "로케트",
    "루콜라": "로케트",
    "로켓": "로케트",
    "그린빈": "그린빈스",
}

_PHONE_RE = re.compile(r"(?<!\d)(01[016789])[-.\s]?(\d{3,4})[-.\s]?(\d{4})(?!\d)")

# 상품명 + 규격/수량. 예: 방토3팩, 바질500g, 와일드1k, 고수 1단
_PRODUCT_RE = re.compile(
    r"(?P<name>[A-Za-z가-힣][A-Za-z가-힣·ㆍ_-]{0,30})\s*"
    r"(?P<spec>\d+(?:\.\d+)?\s*(?:kg|KG|Kg|k|K|키로|킬로|g|G|그램|팩|개|통|단|봉|박스))"
)

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
    r"(?:배송지\s*정보|수취인명|수령인명|받는\s*사람|연락처\s*1|연락처\s*2|연락처|전화번호|"
    r"배송지|주소|상품명|상품\s*목록|상품|배송메모|배송\s*메모|배송메세지|배송메시지|요청사항)\s*[:：-]?",
    re.IGNORECASE,
)
_DATE_NOISE_RE = re.compile(
    r"(?:월|화|수|목|금|토|일)\s*요일|(?:오늘|내일|모레)|\d{1,2}[./-]\d{1,2}(?:[./-]\d{1,2})?|재배송",
    re.IGNORECASE,
)
_MEMO_HINT_RE = re.compile(
    r"문\s*앞|경비실|벨|전화|연락|부재|공동현관|비밀번호|호출|맡겨|놓아|놔|두고|두어|"
    r"말아|주세요|부탁|수령실|택배함|직접|배송메모|배송\s*메모|요청사항",
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


def _normalize_spec(spec: str) -> str:
    s = _compact(spec)
    s = s.replace("킬로", "kg").replace("키로", "kg")
    s = s.replace("그램", "g")
    if re.fullmatch(r"\d+(?:\.\d+)?k", s, re.IGNORECASE):
        s = s[:-1] + "kg"
    s = re.sub(r"kg$", "kg", s, flags=re.IGNORECASE)
    s = re.sub(r"g$", "g", s, flags=re.IGNORECASE)
    return s


def _canonical_from_rules(name: str, mapping_rules: Iterable[Dict]) -> Optional[str]:
    raw = str(name or "").strip()
    raw_compact = _compact(raw)
    if not raw_compact:
        return None

    # 1) 기존 상품명 매칭 규칙을 우선 사용
    for rule in mapping_rules or []:
        if not rule.get("enabled", True):
            continue
        pattern = str(rule.get("pattern", "") or "").strip()
        display = str(rule.get("display_name", "") or "").strip()
        if not pattern or not display:
            continue
        mt = str(rule.get("match_type", "contains") or "contains").strip().lower()
        matched = False
        if mt == "exact":
            matched = _compact(pattern) == raw_compact
        elif mt == "regex":
            try:
                matched = bool(re.search(pattern, raw))
            except re.error:
                matched = False
        else:
            p = _compact(pattern)
            matched = bool(p and (p in raw_compact or raw_compact in p))
        if matched:
            return display

    # 2) 자주 쓰는 축약어
    if raw_compact in _BUILTIN_ALIASES:
        return _BUILTIN_ALIASES[raw_compact]

    # 3) 기존 규칙의 표시명/패턴과 유사도 비교
    candidates: List[str] = []
    for rule in mapping_rules or []:
        for key in ("display_name", "pattern"):
            v = str(rule.get(key, "") or "").strip()
            # 쇼핑몰 상품명 전체보다 짧은 실제 제품명 후보를 우선
            if v and len(_compact(v)) <= 16:
                candidates.append(v)
    candidates.extend(_BUILTIN_ALIASES.values())

    best = None
    best_score = 0.0
    for cand in candidates:
        c = _compact(cand)
        if not c:
            continue
        score = SequenceMatcher(None, raw_compact, c).ratio()
        if score > best_score:
            best_score = score
            best = cand
    if best is not None and best_score >= 0.78:
        return best

    return raw


def _extract_products(text: str, mapping_rules: Iterable[Dict]) -> tuple[List[str], str]:
    products: List[str] = []
    spans = []
    for m in _PRODUCT_RE.finditer(text):
        name = m.group("name")
        spec = m.group("spec")
        canonical = _canonical_from_rules(name, mapping_rules) or name
        product = f"{canonical}{_normalize_spec(spec)}"
        if product not in products:
            products.append(product)
        spans.append(m.span())

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
        r"(?:수취인명|수령인명|받는\s*사람)\s*[:：-]?\s*([가-힣A-Za-z][가-힣A-Za-z .·ㆍ-]{1,29}?)"
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

def _cleanup_leftover(text: str) -> str:
    s = _LABEL_RE.sub(" ", text)
    s = _DATE_NOISE_RE.sub(" ", s)
    s = s.replace("<<PRODUCT>>", " ")
    # 복사/붙여넣기 과정에서 요일 주위에 붙는 반복 따옴표를 배송메모로 남기지 않습니다.
    # 예: 재배송 """"월요일"""" -> 재배송/요일/따옴표 모두 제거
    s = re.sub(r'["“”\'‘’]+', " ", s)
    s = re.sub(r"[,;/|]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -:/")


def _extract_generic_name(leftover: str) -> tuple[str, str]:
    # 메모 문구보다 앞에 있는 짧은 사람 이름을 우선합니다.
    tokens = re.findall(r"[가-힣]{2,6}|[A-Za-z]{2,}(?:\s+[A-Za-z]{2,})?", leftover)
    banned = {
        "배송지", "정보", "연락처", "전화번호", "상품", "목록", "주소", "배송", "메모", "요청사항",
        "문앞", "경비실", "공동현관", "비밀번호", "전화", "연락", "부재", "재배송",
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
        # 이름 후보를 제거한 나머지를 반환
        m = re.search(re.escape(token), leftover)
        if m:
            rest = leftover[: m.start()] + " " + leftover[m.end() :]
        else:
            rest = leftover
        return token.strip(), rest
    return "", leftover


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
    """자유형 재배송 메모를 수취인/연락처/주소/배송메모/상품목록으로 변환합니다."""
    rules = list(mapping_rules or [])
    results: List[Dict[str, str]] = []

    for block in _split_blocks(raw_text):
        original = block
        labeled_name = _extract_labeled_name(original)

        # 전화번호
        phone_match = _PHONE_RE.search(block)
        phone = _normalize_phone(phone_match) if phone_match else ""
        if phone_match:
            block = block[: phone_match.start()] + " " + block[phone_match.end() :]

        # 상품
        products, block = _extract_products(block, rules)
        # 주소 추출의 경계를 잡기 위해 상품 자리에 마커를 넣은 원문도 별도로 사용
        marked = original
        for m in reversed(list(_PRODUCT_RE.finditer(marked))):
            marked = marked[: m.start()] + " <<PRODUCT>> " + marked[m.end() :]
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

        memo = _cleanup_leftover(remaining)
        if memo and len(memo) > 100:
            memo = memo[:100]

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
        prefix = f"{name} - " if name else ""
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
