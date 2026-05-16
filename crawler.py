"""
공모전 사이트 자동 크롤러
지원 사이트: contestkorea, wevity, thinkcontest, detizen
- 당해년도 공모전만 수집
- 각 사이트는 독립적으로 try/except 처리 (한 곳이 실패해도 나머지는 정상 동작)
"""
import asyncio
import re
from datetime import date, datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup


def _current_year() -> int:
    return date.today().year


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# lxml이 없으면 html.parser로 fallback
try:
    import lxml  # noqa
    _PARSER = "lxml"
except ImportError:
    _PARSER = "html.parser"


# ── 태그 분류 ─────────────────────────────────────────────────────────────────

# 키워드 → 앱 태그 매핑 (앱 TAGS 목록: IT/SW, 디자인, 기획·마케팅, 사회혁신, 예술·문화, 창업·스타트업, 논문·학술, 기타)
_TAG_MAP: list[tuple[str, str]] = [
    # IT/SW
    ("IT", "IT/SW"), ("SW", "IT/SW"), ("개발", "IT/SW"), ("소프트웨어", "IT/SW"),
    ("앱", "IT/SW"), ("게임", "IT/SW"), ("인공지능", "IT/SW"), ("AI", "IT/SW"),
    ("빅데이터", "IT/SW"), ("블록체인", "IT/SW"), ("클라우드", "IT/SW"), ("메타버스", "IT/SW"),
    # 디자인
    ("디자인", "디자인"), ("영상", "디자인"), ("사진", "디자인"), ("웹툰", "디자인"),
    ("캐릭터", "디자인"), ("UX", "디자인"), ("UI", "디자인"), ("패션", "디자인"),
    ("제품", "디자인"), ("건축", "디자인"), ("인테리어", "디자인"),
    # 기획·마케팅
    ("기획", "기획·마케팅"), ("마케팅", "기획·마케팅"), ("광고", "기획·마케팅"),
    ("홍보", "기획·마케팅"), ("브랜드", "기획·마케팅"), ("PR", "기획·마케팅"),
    ("아이디어", "기획·마케팅"),
    # 사회혁신
    ("사회", "사회혁신"), ("환경", "사회혁신"), ("공공", "사회혁신"), ("복지", "사회혁신"),
    ("봉사", "사회혁신"), ("ESG", "사회혁신"), ("지속가능", "사회혁신"),
    # 예술·문화
    ("예술", "예술·문화"), ("문화", "예술·문화"), ("음악", "예술·문화"), ("미술", "예술·문화"),
    ("문학", "예술·문화"), ("소설", "예술·문화"), ("공연", "예술·문화"),
    ("무용", "예술·문화"), ("연극", "예술·문화"), ("시나리오", "예술·문화"),
    # 창업·스타트업
    ("창업", "창업·스타트업"), ("스타트업", "창업·스타트업"), ("비즈니스", "창업·스타트업"),
    ("사업", "창업·스타트업"), ("벤처", "창업·스타트업"),
    # 논문·학술
    ("논문", "논문·학술"), ("학술", "논문·학술"), ("연구", "논문·학술"), ("학회", "논문·학술"),
]

# 각 사이트 카테고리명 → 앱 태그 직접 매핑 (빠른 경로)
_SITE_CAT_MAP: dict[str, str] = {
    # 공모전코리아
    "아이디어": "기획·마케팅", "광고/마케팅": "기획·마케팅", "마케팅/광고": "기획·마케팅",
    "IT/SW": "IT/SW", "앱/웹": "IT/SW",
    "디자인/시각": "디자인", "영상/UCC": "디자인", "사진": "디자인", "캐릭터": "디자인",
    "문학/시나리오": "예술·문화", "음악": "예술·문화", "미술": "예술·문화", "공연/연극": "예술·문화",
    "창업/사업계획": "창업·스타트업",
    "논문/리포트": "논문·학술",
    "사회/환경": "사회혁신",
    # 씽크공모전
    "프로그램개발": "IT/SW", "디지털콘텐츠": "IT/SW",
    "디자인": "디자인",
    "광고마케팅": "기획·마케팅",
    "사회공헌": "사회혁신",
    "문화예술": "예술·문화",
    "창업기획": "창업·스타트업",
    "학술논문": "논문·학술",
    # 위비티
    "영상/UCC": "디자인",
}


def _classify_tags(category_text: str) -> list[str]:
    """카테고리 텍스트를 앱 태그 목록으로 분류 (중복 제거)"""
    if not category_text:
        return []
    found: set[str] = set()
    # 직접 매핑 우선
    norm = category_text.strip()
    if norm in _SITE_CAT_MAP:
        found.add(_SITE_CAT_MAP[norm])
        return list(found)
    # 키워드 포함 매핑
    for keyword, tag in _TAG_MAP:
        if keyword in category_text:
            found.add(tag)
    return list(found)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _soup(html_text: str) -> BeautifulSoup:
    return BeautifulSoup(html_text, _PARSER)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(text: str) -> Optional[str]:
    """날짜 문자열을 YYYY-MM-DD로 변환, 파싱 실패 시 None"""
    text = re.sub(r"[^\d.\-/년월일 ]", "", text).strip()
    patterns = [
        r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
        r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일",
        r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                continue
    return None


def _parse_range_date(text: str) -> Optional[str]:
    """
    '접수 04.15~06.17' 또는 '2026-05-18 ~ 2026-07-31' 같은 범위 문자열에서
    마감일(오른쪽, ~ 이후) 파싱
    """
    text = _norm(text)
    if "~" in text:
        text = text.split("~")[-1].strip()

    # 연-월-일 전체 형식 먼저 시도
    full = _parse_date(text)
    if full:
        return full

    # MM.DD 또는 MM/DD (연도 없는 단축 형식)
    m = re.search(r"(\d{1,2})[./](\d{1,2})", text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = _current_year()
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass
    return None


def _is_current_year(deadline_str: Optional[str]) -> bool:
    if not deadline_str:
        return True  # 날짜 파싱 실패 시 포함
    try:
        return datetime.fromisoformat(deadline_str).year >= _current_year()
    except Exception:
        return True


def _item(source: str, source_label: str, title: str, link: str,
          organizer: str = "", deadline: Optional[str] = None,
          prize: str = "", tags: Optional[list] = None) -> dict:
    return {
        "source":       source,
        "source_label": source_label,
        "title":        title,
        "link":         link,
        "organizer":    organizer,
        "deadline":     deadline,
        "prize":        prize,
        "tags":         tags or [],
    }


def _check_response(r: httpx.Response, site: str) -> None:
    """비정상 응답 코드이면 예외 발생"""
    if r.status_code >= 400:
        raise ValueError(f"{site} HTTP {r.status_code}")


# ════════════════════════════════════════════════════════════════════════════
#  사이트별 크롤러
# ════════════════════════════════════════════════════════════════════════════

# ── 1. 공모전코리아 ────────────────────────────────────────────────────────────
# 메인 목록 페이지(전체 분야) 2페이지 수집
# HTML 구조: div.list_style_2 > ul > li
#   li > div.title > a[href] > span.cate (분야), span.txt (제목)
#   li > ul.host > li.icon_1 (주최기관, "주최 · 기관명" 형식)
#   li > div.date > div.date-detail > span.step-1 ("접수 MM.DD~MM.DD" 형식)

_CONTESTKOREA_BASE = (
    "https://www.contestkorea.com/sub/list.php"
    "?int_gbn=1&Txt_sGbn=0&Txt_area=0&Txt_cate=&Txt_bcode="
)


def _parse_contestkorea_items(soup: BeautifulSoup) -> list:
    """파싱된 soup 에서 공모전코리아 항목 추출 (내부 헬퍼)"""
    parsed = []
    items = (
        soup.select("div.list_style_2 > ul > li")
        or soup.select(".list_style_2 li")
        or soup.select("ul.list-type-1 > li")
    )
    for li in items:
        try:
            a = (
                li.select_one("div.title a")
                or li.select_one(".title a")
                or li.select_one("a[href*='view']")
            )
            if not a:
                continue

            # 분야 배지 (.cate) → 앱 태그 분류
            cate_span = a.select_one(".cate") or li.select_one(".cate")
            category = _norm(cate_span.get_text()) if cate_span else ""
            tags = _classify_tags(category)

            # 제목 (.txt 스팬만, 카테고리 스팬 제외)
            txt_span = a.select_one(".txt")
            title = _norm(txt_span.get_text() if txt_span else a.get_text())
            if not title:
                continue

            href = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                # 슬래시가 없으면 반드시 붙여서 도메인+경로 분리
                href = "https://www.contestkorea.com/" + href.lstrip("/")

            # 마감일: span.step-1 "접수 04.15~06.17"
            step1 = li.select_one(".date .step-1") or li.select_one(".date-detail .step-1")
            deadline = _parse_range_date(step1.get_text()) if step1 else None

            # 주최기관
            host_el = li.select_one("ul.host li.icon_1") or li.select_one(".host")
            organizer = ""
            if host_el:
                host_text = _norm(host_el.get_text())
                host_text = re.sub(r"^주최\s*[·.\s]*", "", host_text).strip()
                organizer = host_text

            # 카테고리 매칭 실패 시 제목 키워드로 재시도
            if not tags:
                tags = _classify_tags(title)
            if title and href and _is_current_year(deadline):
                parsed.append(_item("contestkorea", "공모전코리아", title, href, organizer, deadline, tags=tags))
        except Exception:
            continue
    return parsed


async def _crawl_contestkorea(client: httpx.AsyncClient) -> list:
    results = []
    try:
        # 페이지 1, 2 병렬 수집
        urls = [
            _CONTESTKOREA_BASE + "&page=1",
            _CONTESTKOREA_BASE + "&page=2",
        ]
        responses = await asyncio.gather(
            *[client.get(u) for u in urls],
            return_exceptions=True,
        )

        total_li = 0
        for r in responses:
            if isinstance(r, Exception):
                continue
            try:
                _check_response(r, "공모전코리아")
            except Exception:
                continue
            soup = _soup(r.text)
            items = _parse_contestkorea_items(soup)
            total_li += len(soup.select("div.list_style_2 > ul > li") or [])
            results.extend(items)

        if not results:
            results.append({"_error": f"공모전코리아: 항목 파싱 실패 (HTML 구조 변경 가능성, {total_li}개 li 감지)"})
    except Exception as e:
        results.append({"_error": f"공모전코리아 오류: {type(e).__name__}: {e}"})
    return results





# ════════════════════════════════════════════════════════════════════════════
#  메인 진입점
# ════════════════════════════════════════════════════════════════════════════

async def crawl_all() -> dict:
    """
    모든 사이트를 동시에 크롤링하고 결과를 반환합니다.
    반환 형식:
    {
        "items": [{ source, source_label, title, link, organizer, deadline, prize, tags }, ...],
        "errors": ["사이트A 오류: ...", ...],
        "counts": { "사이트": n, ... },
    }
    """
    # 공모주(gongmoju.com): 사이트 접속 불가(ConnectTimeout) → 제외
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=HEADERS,
            verify=True,
        ) as client:
            raw_results = await asyncio.gather(
                _crawl_contestkorea(client),
                return_exceptions=True,
            )
    except Exception as e:
        return {"items": [], "errors": [f"크롤러 초기화 실패: {type(e).__name__}: {e}"], "counts": {}}

    items   = []
    errors  = []
    counts  = {}

    for site_list in raw_results:
        if isinstance(site_list, Exception):
            errors.append(f"{type(site_list).__name__}: {site_list}")
            continue
        if not isinstance(site_list, list):
            continue

        site_items = []
        for item in site_list:
            if "_error" in item:
                errors.append(item["_error"])
            else:
                site_items.append(item)
                label = item.get("source_label", item.get("source", "?"))
                counts[label] = counts.get(label, 0) + 1

        items.extend(site_items)

    # 중복 제거: URL 우선, URL 없으면 (사이트+제목)
    seen  = set()
    dedup = []
    for item in items:
        link = item.get("link", "").strip().rstrip("/")
        key  = link if link else (item.get("source", ""), item.get("title", ""))
        if key not in seen:
            seen.add(key)
            dedup.append(item)

    return {"items": dedup, "errors": errors, "counts": counts}
