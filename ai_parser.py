import base64
import json
import os

from openai import AsyncOpenAI

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SYSTEM_PROMPT = """당신은 공모전 공고문에서 정보를 추출하는 전문가입니다.
주어진 텍스트 또는 이미지에서 공모전 정보를 추출해 JSON 형식으로만 응답하세요.
날짜는 YYYY-MM-DD 형식으로, 없으면 null로 반환하세요.
description은 마크다운으로 보기 좋게 정리하세요. 응모 자격, 주제, 일정, 제출물, 심사 기준이 있으면 포함하세요.
tags는 ["IT/SW","디자인","기획·마케팅","사회혁신","예술·문화","창업·스타트업","논문·학술","기타"] 중 해당하는 값만 선택하세요.

{
  "title": "공모전명",
  "organizer": "주최기관",
  "tags": ["태그1"],
  "start_date": "YYYY-MM-DD 또는 null",
  "deadline": "YYYY-MM-DD 또는 null",
  "announcement_date": "YYYY-MM-DD 또는 null",
  "prize": "상금 및 시상 내용 요약",
  "link": "공식 URL 또는 null",
  "description": "마크다운 형식의 상세 내용"
}"""


def _client() -> AsyncOpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 설정되어 있지 않습니다.")
    return AsyncOpenAI(api_key=api_key)


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
