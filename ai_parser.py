import base64
import io
import json
import os
import struct
import zlib

from typing import Optional

from openai import AsyncOpenAI

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SYSTEM_PROMPT = """당신은 공모전 공고문에서 정보를 추출하는 전문가입니다.
주어진 텍스트 또는 이미지에서 공모전 정보를 추출해 JSON 형식으로만 응답하세요.
날짜는 YYYY-MM-DD 형식으로, 없으면 null로 반환하세요.
description은 마크다운으로 보기 좋게 정리하세요. 응모 자격, 주제, 일정, 제출물, 심사 기준이 있으면 포함하세요.
tags는 ["IT/SW","디자인","기획·마케팅","사회혁신","예술·문화","창업·스타트업","논문·학술","기타"] 중 해당하는 값만 선택하세요.
review_dates는 공모전의 심사/평가 단계별 일정 배열입니다. 1차 심사, 2차 심사, 서류 심사, 발표 심사, 최종 심사 등 공고문에 명시된 심사 일정을 추출하세요. 없으면 빈 배열로 반환하세요.

{
  "title": "공모전명",
  "organizer": "주최기관",
  "tags": ["태그1"],
  "start_date": "YYYY-MM-DD 또는 null",
  "deadline": "YYYY-MM-DD 또는 null",
  "announcement_date": "YYYY-MM-DD 또는 null",
  "review_dates": [{"label": "1차 심사", "date": "YYYY-MM-DD"}],
  "prize": "상금 및 시상 내용 요약",
  "link": "공식 URL 또는 null",
  "description": "마크다운 형식의 상세 내용"
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
