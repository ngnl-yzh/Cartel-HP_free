# 공모전 보드

팀 내부에서 공모전 정보를 등록하고 날짜별로 정리해 보는 FastAPI 웹앱입니다. 관리자는 공모전 공고문 텍스트나 이미지를 GPT로 파싱해 폼에 자동 반영할 수 있고, 팀원은 공개 페이지에서 마감일, 조회수, 태그 기준으로 공모전을 확인합니다.

## 주요 기능

- 공개 메인 페이지: 마감 임박, 조회수, 주목 표시 기준 추천 카드
- 카드형 목록: 태그 필터, 검색, 마감일순, 조회순, 최신순 정렬
- 상세 페이지: 일정, 시상 내용, 공식 링크, 첨부 파일 다운로드
- 관리자 패널: 공모전 추가, 수정, 삭제, 첨부 파일 관리
- GPT 파싱: 텍스트 및 이미지 공고문을 구조화된 입력값으로 변환

## 로컬 실행

```powershell
cd "C:\Users\yzh37\.claude\worktrees\thirsty-leavitt-458609\Desktop\작업\제작 프로그램들\공모전 게시판"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn main:app --reload
```

브라우저에서 `http://127.0.0.1:8000`으로 접속합니다.

## 환경 변수

- `ADMIN_PASSWORD`: 관리자 로그인 비밀번호
- `SECRET_KEY`: 관리자 쿠키 서명용 랜덤 문자열
- `OPENAI_API_KEY`: GPT 파싱 기능용 OpenAI API 키
- `OPENAI_MODEL`: 기본값 `gpt-4o`
- `DATABASE_URL`: Railway PostgreSQL 연결 시 자동 주입 가능
- `UPLOAD_DIR`: 첨부 파일 저장 폴더

## Railway 배포

1. 이 폴더를 GitHub 저장소로 올립니다.
2. Railway에서 새 프로젝트를 만들고 해당 저장소를 연결합니다.
3. Railway 프로젝트에 PostgreSQL 서비스를 추가합니다.
4. 앱 서비스의 Variables에 `DATABASE_URL`을 PostgreSQL 서비스의 `DATABASE_URL`로 연결합니다.
5. Variables에 `ADMIN_PASSWORD`, `SECRET_KEY`, `OPENAI_API_KEY`를 설정합니다.
6. 파일 업로드를 계속 보존하려면 앱 서비스에 Volume을 추가하고 mount path를 `/data`로 설정합니다.
7. 앱 서비스 Variables에 `UPLOAD_DIR=/data/uploads`를 추가합니다.

## 데이터 유지 기준

- `DATABASE_URL`이 PostgreSQL이면 공모전 글, 팀, 회원, 게시글 정보는 GitHub 재배포 후에도 유지됩니다.
- 첨부파일과 이미지는 파일시스템에 저장되므로 Railway Volume이 필요합니다.
- SQLite를 Railway에서 쓸 경우 `DATABASE_URL=sqlite:////data/competitions.db`처럼 Volume 아래에 DB 파일을 둬야 유지됩니다.
- 기존에 Volume 없이 SQLite로 저장한 Railway 데이터는 새 배포나 재시작 때 사라질 수 있으므로 중요한 데이터는 PostgreSQL로 운영하는 것을 권장합니다.
