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
# 실제 HTML 구조: div.list_style_2 > ul > li
#   li > div.title > a[href] > span.txt (제목)
#   li > ul.host > li.icon_1 (주최기관, "주최 . 기관명" 형식)
#   li > div.date > div.date-detail > span.step-1 ("접수 MM.DD~MM.DD" 형식)

async def _crawl_contestkorea(client: httpx.AsyncClient) -> list:
    results = []
    try:
        url = "https://www.contestkorea.com/sub/list.php?int_gbn=1&Txt_sGbn=0&Txt_area=0&Txt_cate=&Txt_bcode=030110001"
        r = await client.get(url)
        _check_response(r, "공모전코리아")
        soup = _soup(r.text)

        # 실제 구조: div.list_style_2 > ul > li
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

                # 제목은 .txt 스팬만 (카테고리 스팬 제외)
                txt_span = a.select_one(".txt")
                title = _norm(txt_span.get_text() if txt_span else a.get_text())
                if not title:
                    continue

                href = a.get("href", "")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.contestkorea.com" + href

                # 마감일: span.step-1 텍스트 "접수 04.15~06.17"
                step1 = li.select_one(".date .step-1") or li.select_one(".date-detail .step-1")
                deadline = _parse_range_date(step1.get_text()) if step1 else None

                # 주최기관: "주최 . 기관명" 에서 "주최 ." 제거
                host_el = li.select_one("ul.host li.icon_1") or li.select_one(".host")
                organizer = ""
                if host_el:
                    host_text = _norm(host_el.get_text())
                    host_text = re.sub(r"^주최\s*[·.\s]*", "", host_text).strip()
                    organizer = host_text

                if title and href and _is_current_year(deadline):
                    results.append(_item("contestkorea", "공모전코리아", title, href, organizer, deadline))
            except Exception:
                continue

        if not results:
            results.append({"_error": f"공모전코리아: 항목 파싱 실패 (HTML 구조 변경 가능성, {len(items)}개 감지)"})
    except Exception as e:
        results.append({"_error": f"공모전코리아 오류: {type(e).__name__}: {e}"})
    return results


# ── 2. 위비티 ─────────────────────────────────────────────────────────────────
# gbn=list 파라미터로 서버렌더링 목록 페이지를 직접 요청
# 구조: div.ms-list > ul.list > li > div.tit a (제목), .organ (기관)

async def _crawl_wevity(client: httpx.AsyncClient) -> list:
    results = []
    try:
        # gbn=c 는 빈 껍데기, gbn=list 가 실제 목록 페이지
        url = "https://www.wevity.com/?c=find&s=1&gbn=list&Txt_bcode=0&Txt_area=0&page=1"
        r = await client.get(url, headers={**HEADERS, "Referer": "https://www.wevity.com/"})
        _check_response(r, "위비티")
        soup = _soup(r.text)

        # div.ms-list > ul.list > li
        items = (
            soup.select("div.ms-list ul.list > li")
            or soup.select("ul.list > li")
            or soup.select(".ms-list li")
        )
        for item in items:
            try:
                tit = item.select_one(".tit a") or item.select_one("a")
                if not tit:
                    continue
                title = _norm(tit.get_text())
                href  = tit.get("href", "")
                if not href or not title:
                    continue
                if not href.startswith("http"):
                    href = "https://www.wevity.com" + href

                org_el = item.select_one(".organ") or item.select_one(".host")
                organizer = _norm(org_el.get_text()) if org_el else ""

                # .day 는 "D-33" 형식 → 날짜 파싱 불가, 날짜 없이 포함
                deadline = None
                date_el = item.select_one(".date") or item.select_one(".period")
                if date_el:
                    deadline = _parse_range_date(date_el.get_text())

                if title and href:
                    results.append(_item("wevity", "위비티", title, href, organizer, deadline))
            except Exception:
                continue

        if not results:
            results.append({"_error": f"위비티: 항목 파싱 실패 (HTML 구조 변경 가능성, {len(items)}개 감지)"})
    except Exception as e:
        results.append({"_error": f"위비티 오류: {type(e).__name__}: {e}"})
    return results


# ── 3. 씽크공모전 ─────────────────────────────────────────────────────────────
# 목록은 POST AJAX API로 로드됨 → HTML 파싱 불가, JSON API 직접 호출
# POST https://www.thinkcontest.com/thinkgood/user/contest/subList.do
# 응답 필드: rows[].contest_pk, program_nm, host_company, receive_period, process_nm

async def _crawl_thinkcontest(client: httpx.AsyncClient) -> list:
    results = []
    try:
        api_url = "https://www.thinkcontest.com/thinkgood/user/contest/subList.do"
        payload = {
            "searchStatus": "Y",
            "sidx": "putup_sdt",
            "sord": "DESC",
            "recordsPerPage": 20,
            "currentPageNo": 1,
        }
        r = await client.post(
            api_url,
            json=payload,
            headers={
                **HEADERS,
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.thinkcontest.com/Contest/List.html",
            },
        )
        _check_response(r, "씽크공모전")

        try:
            data = r.json()
        except Exception as je:
            results.append({"_error": f"씽크공모전: JSON 파싱 실패 ({je})"})
            return results

        # 실제 응답 구조: { "listJsonData": [...], "totalcnt": N, ... }
        rows = (
            data.get("listJsonData")
            or data.get("rows")
            or data.get("list")
            or data.get("data")
            or (data if isinstance(data, list) else [])
        )

        for row in rows:
            try:
                title = _norm(str(row.get("program_nm", "")))
                contest_pk = row.get("contest_pk", "")
                if not title or not contest_pk:
                    continue

                href = f"https://www.thinkcontest.com/thinkgood/user/contest/view.do?contest_pk={contest_pk}"
                organizer = _norm(str(row.get("host_company", "")))

                # "2026-05-18 ~ 2026-07-31" 형식 또는 finish_dt 사용
                period = str(row.get("receive_period", ""))
                deadline = _parse_range_date(period) if period else _parse_date(str(row.get("finish_dt", "")))

                # 마감된 공모전 제외 (status_nm 또는 process_nm 기준)
                status = str(row.get("status_nm", row.get("process_nm", "")))
                if status and "마감" in status and "접수" not in status:
                    continue

                if title and _is_current_year(deadline):
                    results.append(_item("thinkcontest", "씽크공모전", title, href, organizer, deadline))
            except Exception:
                continue

        if not results:
            results.append({"_error": f"씽크공모전: 항목 파싱 실패 (API 응답 0건 또는 모두 마감)"})
    except Exception as e:
        results.append({"_error": f"씽크공모전 오류: {type(e).__name__}: {e}"})
    return results


# ── 4. 데티즌 ─────────────────────────────────────────────────────────────────
# React SPA → HTML 파싱 불가 (body가 <div id="root"></div>)
# 백엔드 REST API를 직접 호출 시도

async def _crawl_detizen(client: httpx.AsyncClient) -> list:
    results = []
    # 알려진 API 엔드포인트 후보
    api_candidates = [
        "https://newdevapi.detizenonline.com/contest?page=0&size=20&sort=endDate,asc",
        "https://newdevapi.detizenonline.com/contests?page=0&size=20",
        "https://newdevapi.detizenonline.com/api/contest?page=0&size=20",
    ]
    api_headers = {
        **HEADERS,
        "Accept": "application/json",
        "Origin": "https://www.detizen.com",
        "Referer": "https://www.detizen.com/",
    }
    try:
        for api_url in api_candidates:
            try:
                r = await client.get(api_url, headers=api_headers)
                if r.status_code != 200:
                    continue
                data = r.json()
                items = (
                    data.get("content")
                    or data.get("data")
                    or (data if isinstance(data, list) else [])
                )
                if not items:
                    continue
                for item in items:
                    try:
                        title = _norm(str(item.get("title", "")))
                        _id   = item.get("_id") or item.get("id", "")
                        if not title or not _id:
                            continue
                        href = f"https://www.detizen.com/mDetail/{_id}"
                        host = item.get("host", {})
                        organizer = _norm(
                            str(host.get("name", "") if isinstance(host, dict) else host)
                        )
                        end_date = str(item.get("endDate", ""))
                        deadline = _parse_date(end_date) if end_date else None
                        if title and _is_current_year(deadline):
                            results.append(_item("detizen", "데티즌", title, href, organizer, deadline))
                    except Exception:
                        continue
                if results:
                    return results
            except Exception:
                continue

        # 모든 API 시도 실패
        results.append({"_error": "데티즌: React SPA (백엔드 API 접근 실패, 수동 등록 필요)"})
    except Exception as e:
        results.append({"_error": f"데티즌 오류: {type(e).__name__}: {e}"})
    return results


# ── 5. 공모주 ─────────────────────────────────────────────────────────────────

async def _crawl_gongmoju(client: httpx.AsyncClient) -> list:
    results = []
    try:
        url = "https://gongmoju.com/wp-json/wp/v2/posts?per_page=30&_embed=1"
        r = await client.get(url, headers={**HEADERS, "Accept": "application/json"})
        if r.status_code == 200:
            try:
                posts = r.json()
                if not isinstance(posts, list):
                    raise ValueError("응답이 배열이 아님")
            except Exception as je:
                results.append({"_error": f"공모주 JSON 파싱 실패: {je}"})
                return results

            for post in posts:
                try:
                    title    = _norm(BeautifulSoup(post.get("title", {}).get("rendered", ""), _PARSER).get_text())
                    href     = post.get("link", "")
                    content_html = post.get("content", {}).get("rendered", "")
                    excerpt  = BeautifulSoup(post.get("excerpt", {}).get("rendered", ""), _PARSER).get_text()

                    deadline = None
                    for text in [content_html, excerpt]:
                        m = re.search(r"마감[^\d]*(\d{4})[.\-/년](\d{1,2})[.\-/월](\d{1,2})", text)
                        if m:
                            try:
                                deadline = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
                                break
                            except ValueError:
                                pass

                    pub_date = post.get("date", "")
                    if pub_date:
                        try:
                            if int(pub_date[:4]) < _current_year():
                                continue
                        except ValueError:
                            pass

                    if title and href:
                        results.append(_item("gongmoju", "공모주", title, href, "", deadline))
                except Exception:
                    continue
        else:
            # JSON API 실패 시 HTML 파싱 시도
            r2 = await client.get("https://gongmoju.com/")
            if r2.status_code == 200:
                soup = _soup(r2.text)
                for item in soup.select("article h2 a, .post h2 a, h2.entry-title a"):
                    try:
                        title = _norm(item.get_text())
                        href  = item.get("href", "")
                        if title and href:
                            results.append(_item("gongmoju", "공모주", title, href))
                    except Exception:
                        continue
            if not results:
                results.append({"_error": f"공모주: API {r.status_code}, HTML 파싱도 결과 없음"})
    except Exception as e:
        results.append({"_error": f"공모주 오류: {type(e).__name__}: {e}"})
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
                _crawl_wevity(client),
                _crawl_thinkcontest(client),
                _crawl_detizen(client),
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

    # 중복 제거 (같은 제목 + 같은 사이트)
    seen  = set()
    dedup = []
    for item in items:
        key = (item.get("source", ""), item.get("title", ""))
        if key not in seen:
            seen.add(key)
            dedup.append(item)

    return {"items": dedup, "errors": errors, "counts": counts}
