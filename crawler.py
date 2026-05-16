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


# ── 완전 차단 키워드: 할인/쿠폰/광고성 이벤트만 차단
_BLOCK_KEYWORDS = re.compile(r'할인|리턴즈|쿠폰|프로모션|특가|사전예약|얼리버드|세일|이벤트할인|무료쿠폰')


def _is_contest_title(title: str) -> bool:
    """완전 차단 여부 — 할인·쿠폰·광고성 이벤트만 False, 나머지는 True."""
    if not title:
        return False
    return not bool(_BLOCK_KEYWORDS.search(title))


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
    """데이콘(dacon.io) — 데이터·AI 공모전 플랫폼
    HTML 구조: div.comp > a[href=/competitions/official/NNNN/overview/] > p.name (제목)
    """
    results = []
    try:
        r = await client.get(
            "https://dacon.io/competitions",
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=25,
        )
        _check_response(r, "데이콘")
        soup = _soup(r.text)

        seen_links: set = set()
        # 실제 HTML 구조: div.comp 내부 a[href] + p.name
        for item in soup.select(".comp"):
            try:
                a = item.select_one("a[href]")
                if not a:
                    continue
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = "https://dacon.io" + href
                if href in seen_links:
                    continue
                seen_links.add(href)

                name_el = item.select_one(".name") or item.select_one("p.ellipsis")
                title = _norm(name_el.get_text() if name_el else a.get_text())
                if len(title) < 4:
                    continue

                # dday: "참가신청중" / "연습" / "D-7" 등 → 날짜 파싱 불가, None 처리
                results.append(_item("dacon", "데이콘", title, href, "", None, tags=["AI/SW"]))
            except Exception:
                continue

        if not results:
            results.append({"_error": "데이콘: 공모전 파싱 실패 (.comp 요소 없음, 구조 변경 가능성)"})

    except Exception as e:
        results.append({"_error": f"데이콘 오류: {type(e).__name__}: {e}"})
    return results


# ── 3. 위비티 ──────────────────────────────────────────────────────────────────

_WEVITY_BASE = "https://www.wevity.com"

async def _crawl_wevity(client: httpx.AsyncClient) -> list:
    """위비티(wevity.com) — 공모전 전문 포털"""
    results = []
    try:
        urls = [
            f"{_WEVITY_BASE}/?c=find&s=1&gotoPage=1&listType=1",
            f"{_WEVITY_BASE}/?c=find&s=1&gotoPage=2&listType=1",
        ]
        responses = await asyncio.gather(
            *[client.get(u, headers=HEADERS) for u in urls],
            return_exceptions=True,
        )
        for r in responses:
            if isinstance(r, Exception):
                continue
            try:
                _check_response(r, "위비티")
            except Exception:
                continue
            soup = _soup(r.text)
            # 위비티 목록: ul.list > li 또는 div.cList > ul > li
            items = (
                soup.select("ul.list > li")
                or soup.select(".cList li")
                or soup.select("div.list-group li")
            )
            for li in items:
                try:
                    a = li.select_one("a[href]")
                    if not a:
                        continue
                    href = a.get("href", "")
                    if href and not href.startswith("http"):
                        href = _WEVITY_BASE + "/" + href.lstrip("/")

                    # 제목
                    tit = (
                        li.select_one(".tit") or li.select_one(".title")
                        or li.select_one("strong") or a
                    )
                    title = _norm(tit.get_text())
                    if not title or len(title) < 3:
                        continue

                    # 분야
                    cate_el = li.select_one(".category") or li.select_one(".cate")
                    category = _norm(cate_el.get_text()) if cate_el else ""
                    tags = _classify_tags(category) or _classify_tags(title)

                    # 마감일
                    date_el = (
                        li.select_one(".date") or li.select_one(".deadline")
                        or li.select_one(".dday")
                    )
                    deadline = _parse_range_date(date_el.get_text()) if date_el else None

                    # 주최
                    host_el = li.select_one(".host") or li.select_one(".organizer")
                    organizer = _norm(host_el.get_text()) if host_el else ""
                    organizer = re.sub(r"^주최\s*[·.\s]*", "", organizer).strip()

                    if title and href and _is_current_year(deadline):
                        results.append(_item("wevity", "위비티", title, href, organizer, deadline, tags=tags))
                except Exception:
                    continue

        if not results:
            results.append({"_error": "위비티: 항목 파싱 실패 (HTML 구조 변경 가능성)"})
    except Exception as e:
        results.append({"_error": f"위비티 오류: {type(e).__name__}: {e}"})
    return results


# ── 4. 링커리어 ────────────────────────────────────────────────────────────────

async def _crawl_linkareer(client: httpx.AsyncClient) -> list:
    """링커리어(linkareer.com) — 공모전·대외활동 플랫폼"""
    results = []
    try:
        # ① API 시도 (링커리어 REST or GraphQL)
        api_tried = False
        for api_url in [
            "https://linkareer.com/api/v1/activities?types=CONTEST&orderBy=LATEST&first=30",
            "https://linkareer.com/api/activity/list?type=contest&page=1&pageSize=30",
        ]:
            try:
                ar = await client.get(api_url, headers={**HEADERS, "Accept": "application/json"}, timeout=10)
                if ar.status_code == 200:
                    import json as _json
                    data = ar.json()
                    # 다양한 응답 구조 탐색
                    comps = (
                        data.get("data") or data.get("results") or
                        data.get("activities") or data.get("list") or []
                    )
                    if isinstance(comps, dict):
                        comps = comps.get("edges") or comps.get("results") or []
                    # GraphQL edge 구조
                    if comps and isinstance(comps[0], dict) and "node" in comps[0]:
                        comps = [c["node"] for c in comps]
                    for comp in (comps if isinstance(comps, list) else []):
                        title = (comp.get("title") or comp.get("name") or "").strip()
                        if not title:
                            continue
                        comp_id = comp.get("id") or comp.get("slug") or ""
                        link = f"https://linkareer.com/activity/{comp_id}" if comp_id else "https://linkareer.com/list/contest"
                        deadline_raw = comp.get("dueDate") or comp.get("deadline") or comp.get("endDate") or ""
                        deadline = _parse_date(str(deadline_raw)) if deadline_raw else None
                        organizer = (comp.get("organization") or comp.get("organizer") or comp.get("host") or "").strip()
                        category = (comp.get("category") or comp.get("type") or "").strip()
                        tags = _classify_tags(category) or _classify_tags(title)
                        if title and _is_current_year(deadline):
                            results.append(_item("linkareer", "링커리어", title, link, organizer, deadline, tags=tags))
                    if results:
                        api_tried = True
                        break
            except Exception:
                continue

        if api_tried and results:
            return results

        # ② HTML 페이지 파싱 (Next.js __NEXT_DATA__ 포함)
        r = await client.get("https://linkareer.com/list/contest", headers=HEADERS, timeout=20)
        _check_response(r, "링커리어")
        soup = _soup(r.text)

        import json as _json
        nxt = soup.find("script", {"id": "__NEXT_DATA__"})
        if nxt and nxt.string:
            try:
                page_data = _json.loads(nxt.string)
                props = page_data.get("props", {}).get("pageProps", {})
                # 링커리어 pageProps 구조 탐색
                comps = []
                for key in ("activities", "list", "data", "contests", "items"):
                    candidate = props.get(key)
                    if isinstance(candidate, list) and candidate:
                        comps = candidate
                        break
                    if isinstance(candidate, dict):
                        inner = candidate.get("edges") or candidate.get("results") or candidate.get("data") or []
                        if inner:
                            comps = inner
                            break
                if comps and isinstance(comps[0], dict) and "node" in comps[0]:
                    comps = [c["node"] for c in comps]
                for comp in comps:
                    title = (comp.get("title") or comp.get("name") or "").strip()
                    if not title:
                        continue
                    comp_id = comp.get("id") or comp.get("slug") or ""
                    link = f"https://linkareer.com/activity/{comp_id}" if comp_id else "https://linkareer.com/list/contest"
                    deadline_raw = comp.get("dueDate") or comp.get("deadline") or comp.get("endDate") or ""
                    deadline = _parse_date(str(deadline_raw)) if deadline_raw else None
                    organizer = (comp.get("organization") or comp.get("organizer") or "").strip()
                    category = (comp.get("category") or comp.get("type") or "").strip()
                    tags = _classify_tags(category) or _classify_tags(title)
                    if title and _is_current_year(deadline):
                        results.append(_item("linkareer", "링커리어", title, link, organizer, deadline, tags=tags))
                if results:
                    return results
            except Exception:
                pass

        # ③ HTML fallback
        for card in soup.select("a[href*='/activity/']"):
            href = card.get("href", "")
            if not href.startswith("http"):
                href = "https://linkareer.com" + href
            tit_el = card.select_one("h2, h3, [class*='title'], [class*='name']") or card
            title = _norm(tit_el.get_text())
            if len(title) < 4 or "링커리어" in title:
                continue
            results.append(_item("linkareer", "링커리어", title, href, "", None, tags=_classify_tags(title)))

        seen = set()
        dedup = []
        for it in results:
            k = it.get("link", it.get("title", ""))
            if k not in seen:
                seen.add(k)
                dedup.append(it)
        results = dedup

        if not results:
            results.append({"_error": "링커리어: 항목 파싱 실패 (JavaScript 렌더링 필요 가능성)"})
    except Exception as e:
        results.append({"_error": f"링커리어 오류: {type(e).__name__}: {e}"})
    return results


# ── 5. 올콘 ───────────────────────────────────────────────────────────────────

async def _crawl_allcon(client: httpx.AsyncClient) -> list:
    """올콘(all-con.co.kr) — 공모전 정보 포털
    HTML 구조(메인 페이지):
      .banner-d-inner  → p.title a[href=/hit/contest/NNNN] + .host + .dday
      .banner-c-inner  → p.title a[href=/hit/contest/NNNN] + .host
    목록 페이지는 JS 렌더링이므로 메인 배너 영역에서 파싱.
    """
    _BASE = "https://www.all-con.co.kr"
    results = []
    try:
        r = await client.get(
            _BASE + "/",
            headers={**HEADERS,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=20,
        )
        _check_response(r, "올콘")
        soup = _soup(r.text)

        seen_links: set = set()
        # banner-d (대형 배너 3개) + banner-c (소형 배너 ~11개)
        for item in soup.select(".banner-d-inner, .banner-c-inner"):
            try:
                # 링크: p.title > a 또는 임의 a[href*=hit/contest]
                a = (
                    item.select_one("p.title a[href]")
                    or item.select_one("a[href*='hit/contest']")
                    or item.select_one("a[href*='view/contest']")
                    or item.select_one("a[href]")
                )
                if not a:
                    continue
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = _BASE + (href if href.startswith("/") else "/" + href)
                if href in seen_links:
                    continue
                seen_links.add(href)

                # 제목: p.title 텍스트 (a 태그 안쪽)
                title_el = item.select_one("p.title")
                title = _norm(title_el.get_text() if title_el else a.get_text())
                if not title or len(title) < 3:
                    continue

                # 주최: .host
                host_el = item.select_one(".host")
                organizer = _norm(host_el.get_text()) if host_el else ""

                # 마감: .dday → "D-8" 형식 — 실제 날짜 알 수 없으므로 None
                # D-0 이하(종료)는 스킵
                dday_el = item.select_one(".dday")
                if dday_el:
                    dday_text = dday_el.get_text(strip=True)
                    m = re.search(r"D-(\d+)", dday_text, re.I)
                    if not m and ("종료" in dday_text or "마감" in dday_text):
                        continue  # 이미 종료

                tags = _classify_tags(title)
                results.append(_item("allcon", "올콘", title, href, organizer, None, tags=tags))
            except Exception:
                continue

        if not results:
            results.append({"_error": "올콘: 항목 파싱 실패 (banner-d-inner/banner-c-inner 없음)"})
    except Exception as e:
        results.append({"_error": f"올콘 오류: {type(e).__name__}: {e}"})
    return results


# ── 6. 온오프믹스 ──────────────────────────────────────────────────────────────

async def _crawl_onoffmix(client: httpx.AsyncClient) -> list:
    """온오프믹스(onoffmix.com) — 이벤트 플랫폼 (공모전 포함)
    HTML 구조: article.event_area > a[href=/event/NNNN]
               .title (제목), .date (기간), .category_type (이벤트 유형)
    검색 URL: /event?s=공모전  (진행 중 공모전 필터)
    대부분 JS 렌더링이므로 진행 중 이벤트가 없으면 빈 결과 반환 (오류 아님).
    """
    _BASE = "https://onoffmix.com"
    results = []
    try:
        # ① 공모전 키워드 검색
        search_urls = [
            f"{_BASE}/event?s=%EA%B3%B5%EB%AA%A8%EC%A0%84",   # 공모전
            f"{_BASE}/event?s=%EA%B2%BD%EC%A7%84%EB%8C%80%ED%9A%8C",  # 경진대회
        ]
        seen_links: set = set()
        for url in search_urls:
            try:
                r = await client.get(url, headers=HEADERS, timeout=20)
                if r.status_code >= 400:
                    continue
                soup = _soup(r.text)
                for article in soup.select("article.event_area"):
                    try:
                        # 종료된 이벤트 스킵
                        if article.select_one(".end_layer"):
                            continue
                        a = article.select_one("a[href]")
                        if not a:
                            continue
                        href = a.get("href", "")
                        if not href.startswith("http"):
                            href = _BASE + (href if href.startswith("/") else "/" + href)
                        if href in seen_links:
                            continue
                        seen_links.add(href)

                        title_el = article.select_one(".title")
                        title = _norm(title_el.get_text() if title_el else a.get_text())
                        if not title or len(title) < 4:
                            continue

                        date_el = article.select_one(".date")
                        deadline = _parse_range_date(date_el.get_text()) if date_el else None

                        tags = _classify_tags(title)
                        results.append(_item("onoffmix", "온오프믹스", title, href, "", deadline, tags=tags))
                    except Exception:
                        continue
            except Exception:
                continue

        # ② 키워드 검색 결과 없으면 여러 interest 페이지에서 제목 필터링
        if not results:
            # 공모전 관련 가능성 높은 interest 코드
            interest_codes = ["A0101", "A0103", "A0104", "A0108"]
            contest_keywords = ["공모전", "경진대회", "공모", "선발", "대회", "contest", "competition"]
            for code in interest_codes:
                try:
                    r = await client.get(
                        f"{_BASE}/event/main/?interest={code}",
                        headers=HEADERS, timeout=20,
                    )
                    if r.status_code >= 400:
                        continue
                    soup = _soup(r.text)
                    for article in soup.select("article.event_area"):
                        try:
                            if article.select_one(".end_layer"):
                                continue
                            title_el = article.select_one(".title")
                            title = _norm(title_el.get_text() if title_el else "")
                            if not any(kw in title for kw in contest_keywords):
                                continue
                            a = article.select_one("a[href]")
                            if not a:
                                continue
                            href = a.get("href", "")
                            if not href.startswith("http"):
                                href = _BASE + (href if href.startswith("/") else "/" + href)
                            if href in seen_links:
                                continue
                            seen_links.add(href)
                            date_el = article.select_one(".date")
                            deadline = _parse_range_date(date_el.get_text()) if date_el else None
                            tags = _classify_tags(title)
                            results.append(_item("onoffmix", "온오프믹스", title, href, "", deadline, tags=tags))
                        except Exception:
                            continue
                except Exception:
                    continue

        # 온오프믹스는 공모전이 없을 수 있으므로 빈 결과는 오류가 아님
    except Exception as e:
        results.append({"_error": f"온오프믹스 오류: {type(e).__name__}: {e}"})
    return results


# ════════════════════════════════════════════════════════════════════════════
#  지원 소스 목록 & 메인 진입점
# ════════════════════════════════════════════════════════════════════════════

# 소스 ID → (표시 이름, 크롤러 함수)
CRAWL_SOURCES: dict = {
    "contestkorea": ("공모전코리아", _crawl_contestkorea),
    "wevity":       ("위비티",       _crawl_wevity),
    "linkareer":    ("링커리어",     _crawl_linkareer),
    "allcon":       ("올콘",         _crawl_allcon),
    "onoffmix":     ("온오프믹스",   _crawl_onoffmix),
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


# ════════════════════════════════════════════════════════════════════════════
#  취업 크롤러 (링커리어 인턴, 사람인 인턴)
# ════════════════════════════════════════════════════════════════════════════

def _job_item(source: str, source_label: str, title: str, company: str,
              link: str, deadline: Optional[str] = None,
              job_type: str = "인턴", location: str = "") -> dict:
    return {
        "source":       source,
        "source_label": source_label,
        "title":        title,
        "company":      company,
        "link":         link,
        "deadline":     deadline,
        "job_type":     job_type,
        "location":     location,
    }


async def crawl_linkareer(client: httpx.AsyncClient) -> list:
    """링커리어(linkareer.com) — 인턴/대외활동/서포터즈 전문"""
    results = []
    try:
        # ① API 시도
        api_urls = [
            "https://linkareer.com/api/v1/activities?types=INTERN&orderBy=LATEST&first=40",
            "https://linkareer.com/api/activity/list?type=intern&page=1&pageSize=40",
        ]
        for api_url in api_urls:
            try:
                ar = await client.get(api_url, headers={**HEADERS, "Accept": "application/json"}, timeout=10)
                if ar.status_code == 200:
                    import json as _json
                    data = ar.json()
                    comps = (data.get("data") or data.get("results") or
                             data.get("activities") or data.get("list") or [])
                    if isinstance(comps, dict):
                        comps = comps.get("edges") or comps.get("results") or []
                    if comps and isinstance(comps[0], dict) and "node" in comps[0]:
                        comps = [c["node"] for c in comps]
                    for comp in (comps if isinstance(comps, list) else []):
                        title = (comp.get("title") or comp.get("name") or "").strip()
                        if not title:
                            continue
                        comp_id = comp.get("id") or comp.get("slug") or ""
                        link = f"https://linkareer.com/activity/{comp_id}" if comp_id else "https://linkareer.com/list/intern"
                        deadline_raw = comp.get("dueDate") or comp.get("deadline") or comp.get("endDate") or ""
                        deadline = _parse_date(str(deadline_raw)) if deadline_raw else None
                        company = (comp.get("organization") or comp.get("organizer") or comp.get("host") or "").strip()
                        activity_type = (comp.get("type") or "").upper()
                        if "SUPPORTER" in activity_type:
                            job_type = "서포터즈"
                        elif "ACTIVITY" in activity_type:
                            job_type = "대외활동"
                        else:
                            job_type = "인턴"
                        if title:
                            results.append(_job_item("linkareer", "링커리어", title, company, link, deadline, job_type))
                    if results:
                        return results
            except Exception:
                continue

        # ② Next.js __NEXT_DATA__ 파싱
        for page_path in ["/list/intern", "/list/activity"]:
            try:
                r = await client.get(f"https://linkareer.com{page_path}", headers=HEADERS, timeout=20)
                _check_response(r, "링커리어")
                soup = _soup(r.text)
                import json as _json
                nxt = soup.find("script", {"id": "__NEXT_DATA__"})
                if nxt and nxt.string:
                    page_data = _json.loads(nxt.string)
                    props = page_data.get("props", {}).get("pageProps", {})
                    comps = []
                    for key in ("activities", "list", "data", "items"):
                        candidate = props.get(key)
                        if isinstance(candidate, list) and candidate:
                            comps = candidate
                            break
                        if isinstance(candidate, dict):
                            inner = candidate.get("edges") or candidate.get("results") or candidate.get("data") or []
                            if inner:
                                comps = inner
                                break
                    if comps and isinstance(comps[0], dict) and "node" in comps[0]:
                        comps = [c["node"] for c in comps]
                    for comp in comps:
                        title = (comp.get("title") or comp.get("name") or "").strip()
                        if not title:
                            continue
                        comp_id = comp.get("id") or comp.get("slug") or ""
                        link = f"https://linkareer.com/activity/{comp_id}" if comp_id else f"https://linkareer.com{page_path}"
                        deadline_raw = comp.get("dueDate") or comp.get("deadline") or comp.get("endDate") or ""
                        deadline = _parse_date(str(deadline_raw)) if deadline_raw else None
                        company = (comp.get("organization") or comp.get("organizer") or "").strip()
                        if "/activity" in page_path:
                            job_type = "대외활동"
                        else:
                            job_type = "인턴"
                        if title:
                            results.append(_job_item("linkareer", "링커리어", title, company, link, deadline, job_type))
                    if results:
                        return results
            except Exception:
                continue

        # ③ HTML fallback
        for page_path, job_type in [("/list/intern", "인턴"), ("/list/activity", "대외활동")]:
            try:
                r = await client.get(f"https://linkareer.com{page_path}", headers=HEADERS, timeout=20)
                if r.status_code >= 400:
                    continue
                soup = _soup(r.text)
                for card in soup.select("a[href*='/activity/']"):
                    href = card.get("href", "")
                    if not href.startswith("http"):
                        href = "https://linkareer.com" + href
                    tit_el = card.select_one("h2, h3, [class*='title'], [class*='name']") or card
                    title = _norm(tit_el.get_text())
                    if len(title) < 4 or "링커리어" in title:
                        continue
                    org_el = card.select_one("[class*='org'], [class*='company'], [class*='organization']")
                    company = _norm(org_el.get_text()) if org_el else ""
                    results.append(_job_item("linkareer", "링커리어", title, company, href, None, job_type))
            except Exception:
                continue

        # 중복 제거
        seen: set = set()
        dedup = []
        for it in results:
            k = it.get("link", it.get("title", ""))
            if k not in seen:
                seen.add(k)
                dedup.append(it)
        results = dedup

        if not results:
            results.append({"_error": "링커리어: 취업 공고 파싱 실패 (JavaScript 렌더링 필요 가능성)"})
    except Exception as e:
        results.append({"_error": f"링커리어 취업 오류: {type(e).__name__}: {e}"})
    return results


async def crawl_saramin_intern(client: httpx.AsyncClient) -> list:
    """사람인(saramin.co.kr) — 인턴 공고"""
    results = []
    try:
        url = (
            "https://www.saramin.co.kr/zf_user/jobs/list/job-category"
            "?cat_kewd=2232&loc_mcd=101000&education=0"
            "&panel_type=&search_optional_item=n&search_done=y"
            "&panel_count=y&preview=y"
        )
        r = await client.get(url, headers=HEADERS, timeout=25)
        _check_response(r, "사람인")
        soup = _soup(r.text)

        _SARAMIN_BASE = "https://www.saramin.co.kr"
        items = soup.select(".list_recruit .item_recruit")
        if not items:
            items = soup.select(".list-jobs .list-item")  # fallback 선택자

        for item in items:
            try:
                # 제목
                tit_el = item.select_one(".job_tit a") or item.select_one("a.job_tit") or item.select_one("h2 a")
                if not tit_el:
                    continue
                title = _norm(tit_el.get_text())
                if not title:
                    continue
                href = tit_el.get("href", "")
                if href and not href.startswith("http"):
                    href = _SARAMIN_BASE + href

                # 회사명
                corp_el = item.select_one(".corp_name a") or item.select_one(".company_name a") or item.select_one(".company a")
                company = _norm(corp_el.get_text()) if corp_el else ""

                # 마감일
                date_el = item.select_one(".job_date .date") or item.select_one(".deadline") or item.select_one(".date")
                deadline = _parse_date(date_el.get_text()) if date_el else None

                # 근무지
                loc_el = item.select_one(".work_place") or item.select_one(".location")
                location = _norm(loc_el.get_text()) if loc_el else ""

                if title and href:
                    results.append(_job_item("saramin", "사람인", title, company, href, deadline, "인턴", location))
            except Exception:
                continue

        if not results:
            results.append({"_error": "사람인: 인턴 공고 파싱 실패 (HTML 구조 변경 가능성)"})
    except Exception as e:
        results.append({"_error": f"사람인 오류: {type(e).__name__}: {e}"})
    return results


# ── 취업 소스 목록 ──────────────────────────────────────────────────────────────
JOB_SOURCES: dict = {
    "linkareer": ("링커리어", crawl_linkareer),
    "saramin":   ("사람인",   crawl_saramin_intern),
}


async def run_job_crawlers(sources: list = None) -> dict:
    """
    지정된 취업 소스(기본: 전체)를 동시에 크롤링하고 결과를 반환합니다.
    반환 형식:
    {
        "items": [{ source, source_label, title, company, link, deadline, job_type, location }, ...],
        "errors": ["사이트A 오류: ...", ...],
        "counts": { "사이트": n, ... },
    }
    """
    valid_sources = [s for s in (sources or list(JOB_SOURCES.keys())) if s in JOB_SOURCES]
    if not valid_sources:
        return {"items": [], "errors": ["선택된 취업 크롤링 소스가 없습니다."], "counts": {}}

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=HEADERS,
            verify=True,
        ) as client:
            raw_results = await asyncio.gather(
                *[JOB_SOURCES[s][1](client) for s in valid_sources],
                return_exceptions=True,
            )
    except Exception as e:
        return {"items": [], "errors": [f"취업 크롤러 초기화 실패: {type(e).__name__}: {e}"], "counts": {}}

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

    # 중복 제거
    seen  = set()
    dedup = []
    for item in items:
        link = item.get("link", "").strip().rstrip("/")
        key  = link if link else (item.get("source", ""), item.get("title", ""))
        if key not in seen:
            seen.add(key)
            dedup.append(item)

    return {"items": dedup, "errors": errors, "counts": counts}
