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
from urllib.parse import urljoin as _urljoin

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


# ── 공모전코리아 분야 카테고리 ────────────────────────────────────────────────
# 공모전코리아 사이트의 실제 대분류를 그대로 사용
CONTESTKOREA_CATS = [
    "문학•문예",
    "네이밍•슬로건",
    "학문•과학•IT",
    "AI/SW",               # 공모전코리아 외 별도 분류 (AI·소프트웨어 특화)
    "미술•디자인•웹툰",
    "사진•영상•영화제",
    "음악•콩쿠르•댄스",
    "아이디어•건축•창업",
    "스포츠",
    "요리•뷰티•배우•오디션",
    "기타",
]

# 카테고리별 키워드 (부분 매칭용 안전망)
# AI/SW는 공모전코리아에 없으므로 크롤링 시 자동 분류 대상에서 제외(빈 리스트),
# 단 제목/카테고리에 명확한 AI/SW 키워드가 있으면 매핑
_CAT_KEYWORDS: list[tuple[list[str], str]] = [
    (["문학", "시나리오", "소설", "수필", "동화", "시", "문예"], "문학•문예"),
    (["네이밍", "슬로건", "캐치프레이즈"], "네이밍•슬로건"),
    # AI/SW 먼저 체크 (학문•과학•IT보다 구체적인 키워드)
    (["인공지능", "머신러닝", "딥러닝", "LLM", "생성형 AI", "ChatGPT", "소프트웨어", "SW", "앱 개발", "게임 개발"], "AI/SW"),
    (["학문", "과학", "IT", "개발", "AI", "데이터", "앱", "웹", "게임", "빅데이터", "클라우드"], "학문•과학•IT"),
    (["미술", "디자인", "웹툰", "캐릭터", "UX", "UI", "패션", "제품", "건축", "인테리어"], "미술•디자인•웹툰"),
    (["사진", "영상", "영화", "UCC", "다큐", "촬영"], "사진•영상•영화제"),
    (["음악", "콩쿠르", "댄스", "무용", "공연", "연극", "뮤지컬"], "음악•콩쿠르•댄스"),
    (["아이디어", "건축", "창업", "스타트업", "비즈니스", "사업계획"], "아이디어•건축•창업"),
    (["스포츠", "체육", "운동"], "스포츠"),
    (["요리", "뷰티", "배우", "오디션", "헤어", "메이크업"], "요리•뷰티•배우•오디션"),
]


def _normalize_cat(text: str) -> str:
    """카테고리 구분자(•·・) 통일 및 공백 제거"""
    return re.sub(r"[•·・]", "•", text.strip())


def _classify_tags(category_text: str) -> list[str]:
    """공모전코리아 카테고리 텍스트를 CONTESTKOREA_CATS 목록으로 분류"""
    if not category_text:
        return []
    norm = _normalize_cat(category_text)
    # 직접 일치 (span.category 값이 대부분 정확히 일치)
    for cat in CONTESTKOREA_CATS:
        if norm == _normalize_cat(cat):
            return [cat]
    # 키워드 포함 매핑 (안전망)
    for keywords, cat in _CAT_KEYWORDS:
        for kw in keywords:
            if kw in category_text:
                return [cat]
    return []


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

            # 분야: span.category (공모전코리아 실제 HTML 구조)
            cate_span = (
                a.select_one(".category") or li.select_one(".category")
                or a.select_one(".cate") or li.select_one(".cate")  # fallback
            )
            category = _norm(cate_span.get_text()) if cate_span else ""
            tags = _classify_tags(category)

            # 제목: span.txt (카테고리 스팬 제외)
            txt_span = a.select_one(".txt")
            title = _norm(txt_span.get_text() if txt_span else a.get_text())
            if not title:
                continue

            href = a.get("href", "")
            if not href:
                continue
            # urljoin으로 상대경로 해결 (view.php?... → /sub/view.php?...)
            # 절대 URL이어도 안전하게 처리됨
            href = _urljoin("https://www.contestkorea.com/sub/list.php", href)
            # www 없는 도메인 정규화 (contestkorea.com → www.contestkorea.com)
            href = href.replace("://contestkorea.com/", "://www.contestkorea.com/")

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





# ── 2. 데이콘 ──────────────────────────────────────────────────────────────────

async def _crawl_dacon(client: httpx.AsyncClient) -> list:
    """데이콘(dacon.io) — 데이터·AI 공모전 플랫폼"""
    results = []
    try:
        r = await client.get(
            "https://dacon.io/competitions",
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=20,
        )
        _check_response(r, "데이콘")
        soup = _soup(r.text)

        # ① Next.js __NEXT_DATA__ JSON 추출 시도
        import json as _json
        nxt = soup.find("script", {"id": "__NEXT_DATA__"})
        if nxt and nxt.string:
            try:
                page_data = _json.loads(nxt.string)
                props = page_data.get("props", {}).get("pageProps", {})
                comps = (
                    props.get("competitions")
                    or props.get("data")
                    or props.get("list")
                    or []
                )
                if isinstance(comps, dict):
                    comps = comps.get("results") or comps.get("data") or []
                for comp in (comps if isinstance(comps, list) else []):
                    title = (comp.get("title") or comp.get("name") or "").strip()
                    if not title:
                        continue
                    comp_id = comp.get("id") or ""
                    link = f"https://dacon.io/competitions/{comp_id}" if comp_id else "https://dacon.io/competitions"
                    deadline_raw = (
                        comp.get("end_date") or comp.get("deadline")
                        or comp.get("finish_date") or comp.get("endDate") or ""
                    )
                    deadline = _parse_date(str(deadline_raw)) if deadline_raw else None
                    organizer = (comp.get("host") or comp.get("organizer") or "").strip()
                    prize = str(comp.get("prize") or comp.get("reward") or "").strip()
                    if title and _is_current_year(deadline):
                        results.append(_item("dacon", "데이콘", title, link, organizer, deadline, prize, tags=["AI/SW"]))
                if results:
                    return results
            except Exception:
                pass

        # ② HTML fallback — /competitions/숫자 형태 링크 추출
        seen_links: set = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not re.search(r"/competitions/\d+", href):
                continue
            if not href.startswith("http"):
                href = "https://dacon.io" + href
            if href in seen_links:
                continue
            seen_links.add(href)
            title_el = (
                a.select_one("h2, h3, h4, [class*='tit'], [class*='title'], [class*='name']")
                or a
            )
            title = _norm(title_el.get_text())
            if len(title) < 4:
                continue
            results.append(_item("dacon", "데이콘", title, href, "", None, tags=["AI/SW"]))

        if not results:
            results.append({"_error": "데이콘: 공모전 파싱 실패 (JavaScript 렌더링 또는 구조 변경)"})

    except Exception as e:
        results.append({"_error": f"데이콘 오류: {type(e).__name__}: {e}"})
    return results


# ════════════════════════════════════════════════════════════════════════════
#  지원 소스 목록 & 메인 진입점
# ════════════════════════════════════════════════════════════════════════════

# 소스 ID → (표시 이름, 크롤러 함수)
CRAWL_SOURCES: dict = {
    "contestkorea": ("공모전코리아", _crawl_contestkorea),
    "dacon":        ("데이콘",       _crawl_dacon),
}


async def crawl_all(sources: list = None) -> dict:
    """
    지정된 소스(기본: 전체)를 동시에 크롤링하고 결과를 반환합니다.
    반환 형식:
    {
        "items": [{ source, source_label, title, link, organizer, deadline, prize, tags }, ...],
        "errors": ["사이트A 오류: ...", ...],
        "counts": { "사이트": n, ... },
    }
    """
    valid_sources = [s for s in (sources or list(CRAWL_SOURCES.keys())) if s in CRAWL_SOURCES]
    if not valid_sources:
        return {"items": [], "errors": ["선택된 크롤링 소스가 없습니다."], "counts": {}}

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=HEADERS,
            verify=True,
        ) as client:
            raw_results = await asyncio.gather(
                *[CRAWL_SOURCES[s][1](client) for s in valid_sources],
                return_exceptions=True,
            )
    except Exception as e:
        return {"items": [], "errors": [f"크롤러 초기화 실패: {type(e).__name__}: {e}"], "counts": {}}

    items  = []
    errors = []
    counts = {}

    for site_list in raw_results:
        if isinstance(site_list, Exception):
            errors.append(f"{type(site_list).__name__}: {site_list}")
            continue
        if not isinstance(site_list, list):
            continue
        for item in site_list:
            if "_error" in item:
                errors.append(item["_error"])
            else:
                items.append(item)
                label = item.get("source_label", item.get("source", "?"))
                counts[label] = counts.get(label, 0) + 1

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
