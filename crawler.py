"""
공모전 사이트 자동 크롤러
지원 사이트: contestkorea, wevity, thinkcontest, detizen, gongmoju
- 당해년도 공모전만 수집
- 각 사이트는 독립적으로 try/except 처리 (한 곳이 실패해도 나머지는 정상 동작)
"""
import asyncio
import re
from datetime import date, datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

CURRENT_YEAR = date.today().year

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

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


def _is_current_year(deadline_str: Optional[str]) -> bool:
    if not deadline_str:
        return True  # 날짜 파싱 실패 시 포함
    try:
        return datetime.fromisoformat(deadline_str).year >= CURRENT_YEAR
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


# ════════════════════════════════════════════════════════════════════════════
#  사이트별 크롤러
# ════════════════════════════════════════════════════════════════════════════

# ── 1. 공모전코리아 ────────────────────────────────────────────────────────────

async def _crawl_contestkorea(client: httpx.AsyncClient) -> list[dict]:
    results = []
    try:
        url = "https://www.contestkorea.com/sub/list.php?int_gbn=1&Txt_sGbn=0&Txt_area=0&Txt_cate=&Txt_bcode=030110001"
        r = await client.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        for li in soup.select("ul.list-type-1 li, div.list_wrap .list_con"):
            try:
                a = li.select_one("a.btn.btn-link, strong.tit a, .tit a, h4 a")
                if not a:
                    continue
                title = _norm(a.get_text())
                href  = a.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.contestkorea.com" + href

                # 날짜
                date_el = li.select_one(".date, .dday, span.day")
                deadline = None
                if date_el:
                    deadline = _parse_date(date_el.get_text())

                # 주최
                org_el = li.select_one(".host, .organ, .name_organ")
                organizer = _norm(org_el.get_text()) if org_el else ""

                if title and href and _is_current_year(deadline):
                    results.append(_item("contestkorea", "공모전코리아", title, href, organizer, deadline))
            except Exception:
                continue
    except Exception as e:
        results.append({"_error": f"공모전코리아 오류: {e}"})
    return results


# ── 2. 위비티 ─────────────────────────────────────────────────────────────────

async def _crawl_wevity(client: httpx.AsyncClient) -> list[dict]:
    results = []
    try:
        url = "https://www.wevity.com/?c=find&s=1&gbn=c&Txt_bcode=0&Txt_area=0&Txt_pri=&page=1"
        r = await client.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        for item in soup.select("ul.contest-list > li, .find-list li"):
            try:
                a = item.select_one("a")
                if not a:
                    continue
                title = _norm(a.get_text())
                href  = a.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.wevity.com" + href

                date_el = item.select_one(".dday, .date, .deadline")
                deadline = _parse_date(date_el.get_text()) if date_el else None

                org_el = item.select_one(".organ, .host, .company")
                organizer = _norm(org_el.get_text()) if org_el else ""

                prize_el = item.select_one(".prize, .award")
                prize = _norm(prize_el.get_text()) if prize_el else ""

                if title and href and _is_current_year(deadline):
                    results.append(_item("wevity", "위비티", title, href, organizer, deadline, prize))
            except Exception:
                continue
    except Exception as e:
        results.append({"_error": f"위비티 오류: {e}"})
    return results


# ── 3. 씽크공모전 ─────────────────────────────────────────────────────────────

async def _crawl_thinkcontest(client: httpx.AsyncClient) -> list[dict]:
    results = []
    try:
        url = "https://www.thinkcontest.com/Contest/List.html?pCd=c01"
        r = await client.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        for item in soup.select("ul.listS > li, .contest_list li, .list_con li"):
            try:
                a = item.select_one("a")
                if not a:
                    continue
                title = _norm(a.get_text())
                href  = a.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.thinkcontest.com" + href

                date_el = item.select_one(".date, .day, .period, span[class*=date]")
                deadline = None
                if date_el:
                    deadline = _parse_date(date_el.get_text())

                org_el = item.select_one(".host, .organ, .company")
                organizer = _norm(org_el.get_text()) if org_el else ""

                if title and href and _is_current_year(deadline):
                    results.append(_item("thinkcontest", "씽크공모전", title, href, organizer, deadline))
            except Exception:
                continue
    except Exception as e:
        results.append({"_error": f"씽크공모전 오류: {e}"})
    return results


# ── 4. 데티즌 ─────────────────────────────────────────────────────────────────

async def _crawl_detizen(client: httpx.AsyncClient) -> list[dict]:
    results = []
    try:
        url = "https://www.detizen.com/contest/list"
        r = await client.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        for item in soup.select("ul.bbs_list > li, .contest-item, .list-item"):
            try:
                a = item.select_one("a")
                if not a:
                    continue
                title = _norm(a.get_text())
                href  = a.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.detizen.com" + href

                date_el = item.select_one(".date, .deadline, .period, .dday")
                deadline = _parse_date(date_el.get_text()) if date_el else None

                org_el = item.select_one(".host, .organ, .company, .org")
                organizer = _norm(org_el.get_text()) if org_el else ""

                if title and href and _is_current_year(deadline):
                    results.append(_item("detizen", "데티즌", title, href, organizer, deadline))
            except Exception:
                continue
    except Exception as e:
        results.append({"_error": f"데티즌 오류: {e}"})
    return results


# ── 5. 공모주 ─────────────────────────────────────────────────────────────────

async def _crawl_gongmoju(client: httpx.AsyncClient) -> list[dict]:
    results = []
    try:
        url = "https://gongmoju.com/wp-json/wp/v2/posts?per_page=30&_embed=1"
        r = await client.get(url, headers={**HEADERS, "Accept": "application/json"})
        if r.status_code == 200:
            posts = r.json()
            for post in posts:
                try:
                    title    = _norm(BeautifulSoup(post.get("title", {}).get("rendered", ""), "lxml").get_text())
                    href     = post.get("link", "")
                    content_html = post.get("content", {}).get("rendered", "")
                    excerpt  = BeautifulSoup(post.get("excerpt", {}).get("rendered", ""), "lxml").get_text()

                    # 날짜 추출 — 본문에서 마감일 패턴 찾기
                    deadline = None
                    for text in [content_html, excerpt]:
                        m = re.search(r"마감[^\d]*(\d{4})[.\-/년](\d{1,2})[.\-/월](\d{1,2})", text)
                        if m:
                            try:
                                deadline = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
                                break
                            except ValueError:
                                pass

                    # 게시일로 올해 필터
                    pub_date = post.get("date", "")
                    if pub_date:
                        pub_year = int(pub_date[:4])
                        if pub_year < CURRENT_YEAR:
                            continue

                    if title and href:
                        results.append(_item("gongmoju", "공모주", title, href, "", deadline))
                except Exception:
                    continue
        else:
            # JSON API 실패 시 HTML 파싱 시도
            r2 = await client.get("https://gongmoju.com/")
            soup = BeautifulSoup(r2.text, "lxml")
            for item in soup.select("article, .post, h2.entry-title"):
                try:
                    a = item.select_one("a")
                    if not a:
                        continue
                    title = _norm(a.get_text())
                    href  = a.get("href", "")
                    if title and href and _is_current_year(None):
                        results.append(_item("gongmoju", "공모주", title, href))
                except Exception:
                    continue
    except Exception as e:
        results.append({"_error": f"공모주 오류: {e}"})
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
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:
        raw_results = await asyncio.gather(
            _crawl_contestkorea(client),
            _crawl_wevity(client),
            _crawl_thinkcontest(client),
            _crawl_detizen(client),
            _crawl_gongmoju(client),
            return_exceptions=True,
        )

    items   = []
    errors  = []
    counts  = {}

    for site_list in raw_results:
        if isinstance(site_list, Exception):
            errors.append(str(site_list))
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

    # 중복 제거 (같은 제목 + 같은 사이트)
    seen  = set()
    dedup = []
    for item in items:
        key = (item["source"], item["title"])
        if key not in seen:
            seen.add(key)
            dedup.append(item)

    return {"items": dedup, "errors": errors, "counts": counts}
