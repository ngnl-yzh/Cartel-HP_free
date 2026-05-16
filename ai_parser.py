import base64
import io
import json
import os
import struct
import zlib

from typing import Optional

from openai import AsyncOpenAI

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SYSTEM_PROMPT = """당신은 한국 공모전 공고문 전문 파싱 AI입니다.
주어진 텍스트·이미지에서 공모전 정보를 추출해 JSON 형식으로만 응답하세요. 설명 문장 없이 JSON만 출력하세요.

## 날짜 규칙
- 반드시 YYYY-MM-DD 형식. 연도 없으면 문맥에서 추론하거나 현재 연도 사용.
- 지원하는 형식: "2026.05.18", "26.5.18", "2026년 5월 18일", "5/18", "'26. 5. 18.(월)"
- "추후 공지", "미정", "TBD" 처럼 아무 날짜도 추론 불가한 경우만 null.
- 날짜 범위 "A ~ B"에서 deadline은 B(마지막 날짜), start_date는 A(첫 날짜).

## 필드별 추출 우선순위
텍스트 앞부분에 【공모전 정보】 블록이 있으면 해당 데이터를 최우선으로 사용하세요.

- **deadline**: 【공모전 정보】의 "접수기간" 마감일 > 본문의 "접수 마감" / "응모 마감" / "제출 마감"
- **start_date**: 【공모전 정보】의 "접수기간" 시작일 > 본문의 "공고일" / "접수 시작"
- **announcement_date**: 【공모전 정보】에 "발표" 항목 > 본문의 "결과 발표" / "당선 발표". **특정 날짜가 없으면 null.**
- **review_dates**: 단계별 심사 일정. 없으면 [].
  - 【공모전 정보】의 "심사기간", 본문의 "1차 심사", "서류 심사", "발표 심사", "결과 발표" 등 포함.
  - **date 필드 규칙: 명확한 날짜(연·월·일 모두 확정)이면 YYYY-MM-DD로 변환. 그 외에는 원문 표기 그대로.**
    예) "2026.07.14" → "2026-07-14" / "7월 말" → "7월 말" / "8월 중순" → "8월 중순" / "8월 말 ~ 9월 초" → "8월 말 ~ 9월 초"
  - "추후 공지", "미정" 등 날짜 정보가 전혀 없으면 생략.

## 기타 규칙
- **prize**: 시상 내역 간결 요약. 예) "최우수 300만원, 우수 200만원, 장려 100만원"
- **tags**: ["문학•문예","네이밍•슬로건","학문•과학•IT","AI/SW","미술•디자인•웹툰","사진•영상•영화제","음악•콩쿠르•댄스","아이디어•건축•창업","스포츠","요리•뷰티•배우•오디션","기타"] 중 해당하는 것만
  - AI/SW: 인공지능·머신러닝·딥러닝·소프트웨어 개발·앱 개발 등에 특화된 공모전
- **organizer**: 주최·주관 기관명. 여럿이면 쉼표로 구분.
- **description**: 마크다운. 응모 자격·주제·제출물·심사 기준·특이사항 포함. 최대 1500자.
  - AI 요약·SNS 공유 안내·오류제보 문구 등 불필요한 내용은 제외.

{
  "title": "공모전명",
  "organizer": "주최·주관기관",
  "tags": ["태그"],
  "start_date": "YYYY-MM-DD 또는 null",
  "deadline": "YYYY-MM-DD 또는 null",
  "announcement_date": "YYYY-MM-DD 또는 null",
  "review_dates": [
    {"label": "심사기간", "date": "2026-07-14"},
    {"label": "서류·발표평가", "date": "7월 말 ~ 8월 중순"},
    {"label": "결과 발표", "date": "8월 말 ~ 9월 초"}
  ],
  "prize": "시상 내역 요약",
  "link": "공식 URL 또는 null",
  "description": "마크다운 상세 내용"
}"""

# 싱글턴 클라이언트 — 매 호출마다 생성하지 않음
_openai_client: Optional[AsyncOpenAI] = None


def _client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY가 설정되어 있지 않습니다.")
        _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


async def parse_text(text: str) -> dict:
    response = await _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


async def parse_image_file(image_data: bytes, content_type: str | None) -> dict:
    encoded = base64.b64encode(image_data).decode()
    data_url = f"data:{content_type or 'image/png'};base64,{encoded}"

    response = await _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "이 이미지에서 공모전 정보를 추출하세요."},
                ],
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


# ── 문서 파싱 (PDF / HWP / HWPX) ─────────────────────────────────────────────

def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        return "\n".join(parts)
    except Exception as exc:
        raise RuntimeError(f"PDF 텍스트 추출 실패: {exc}") from exc


def _extract_hwp_text(file_bytes: bytes) -> str:
    """HWP 5.x 바이너리(OLE2) 파일에서 텍스트를 추출합니다."""
    try:
        import olefile
    except ImportError:
        raise RuntimeError("olefile 패키지가 필요합니다: pip install olefile")

    try:
        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
        text_parts: list[str] = []
        section_idx = 0

        while True:
            stream_name = f"BodyText/Section{section_idx}"
            if not ole.exists(stream_name):
                break
            raw = ole.openstream(stream_name).read()

            # 압축 해제 (deflate raw)
            try:
                raw = zlib.decompress(raw, -15)
            except zlib.error:
                pass

            pos = 0
            while pos + 4 <= len(raw):
                header = struct.unpack_from("<I", raw, pos)[0]
                tag_id = header & 0x3FF
                size = (header >> 20) & 0xFFF
                pos += 4
                if size == 0xFFF:
                    if pos + 4 <= len(raw):
                        size = struct.unpack_from("<I", raw, pos)[0]
                        pos += 4
                    else:
                        break

                # HWPTAG_PARA_TEXT = 67
                if tag_id == 67:
                    try:
                        chunk = raw[pos: pos + size]
                        text = chunk.decode("utf-16-le", errors="ignore").rstrip("\x00")
                        if text:
                            text_parts.append(text)
                    except Exception:
                        pass

                pos += size

            section_idx += 1

        ole.close()
        return "\n".join(text_parts)

    except Exception as exc:
        raise RuntimeError(f"HWP 텍스트 추출 실패: {exc}") from exc


def _extract_hwpx_text(file_bytes: bytes) -> str:
    """HWPX (ZIP 기반 새 한글 형식)에서 텍스트를 추출합니다."""
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
        text_parts: list[str] = []

        for name in zf.namelist():
            lower = name.lower()
            if "section" in lower and lower.endswith(".xml"):
                try:
                    content = zf.read(name).decode("utf-8", errors="ignore")
                    root = ET.fromstring(content)
                    for elem in root.iter():
                        if elem.text and elem.text.strip():
                            text_parts.append(elem.text.strip())
                except Exception:
                    pass

        zf.close()
        return "\n".join(text_parts)

    except Exception as exc:
        raise RuntimeError(f"HWPX 텍스트 추출 실패: {exc}") from exc


async def parse_document_file(file_bytes: bytes, filename: str) -> dict:
    """PDF / HWP / HWPX 파일에서 텍스트를 추출한 뒤 GPT로 공모전 정보를 파싱합니다."""
    fname = filename.lower()

    if fname.endswith(".pdf"):
        text = _extract_pdf_text(file_bytes)
    elif fname.endswith(".hwpx"):
        text = _extract_hwpx_text(file_bytes)
    elif fname.endswith(".hwp"):
        text = _extract_hwp_text(file_bytes)
    else:
        raise ValueError(f"지원하지 않는 파일 형식입니다. (PDF, HWP, HWPX만 가능)")

    text = text.strip()
    if not text:
        raise ValueError("문서에서 텍스트를 추출할 수 없습니다. 스캔된 이미지 PDF이면 이미지 파싱을 사용하세요.")

    # 토큰 제한을 위해 앞 8000자만 사용
    return await parse_text(text[:8000])
