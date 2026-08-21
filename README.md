# 하쿠슈 갤러리 리뷰 백업기

디시인사이드 `하쿠슈` 마이너 갤러리에서 `리뷰✍️` 말머리(`search_head=80`)가 붙은 게시글만 GitHub Pages용 정적 사이트로 저장합니다.

- 제목, 말머리, 작성자, 작성일, 본문, 본문 이미지와 댓글을 저장합니다.
- 댓글에 포함된 디시콘과 이미지도 함께 저장합니다.
- 다시 실행하면 이미 저장한 글은 건너뛰고 새 글만 추가합니다.
- 전체 목록에서 각 리뷰로 이동할 수 있고, 리뷰 안에서 목록·앞 글·뒤 글로 이동할 수 있습니다.
- 이미지와 내부 링크는 GitHub Pages에서 동작하는 상대경로로 생성됩니다.

## 설치

Python 3.10 이상을 권장합니다.

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## 실행

```bash
python archive.py
```

기본 출력 폴더는 현재 위치의 `docs`입니다. 완료 후 `docs/index.html`을 브라우저로 열면 됩니다.

출력 위치 지정:

```bash
python archive.py --output docs
```

이미 저장한 글까지 다시 갱신:

```bash
python archive.py --refresh
```

이미지를 받지 않고 글과 댓글 텍스트만 저장:

```bash
python archive.py --no-images
```

댓글을 제외하려면:

```bash
python archive.py --no-comments
```

시험 삼아 최신 글 3개만 저장:

```bash
python archive.py --limit-posts 3 --max-pages 1
```

전체 옵션:

```bash
python archive.py --help
```

## 저장 구조

```text
docs/
├── .nojekyll
├── index.html
├── manifest.json
└── posts/
    └── 0000005378_게시글_제목/
        ├── index.html
        ├── content.html
        ├── content.txt
        ├── comments.json
        ├── comments.txt
        ├── post.json
        └── assets/
            ├── body/
            └── comments/
```

`manifest.json`은 증분 백업 상태 파일입니다. 삭제하면 다음 실행에서 기존 글을 다시 받습니다.

GitHub 저장소의 Pages 설정에서 `Deploy from a branch` → `main` → `/docs`를 선택하면 됩니다.

## 주의

- 기본 요청 간격은 1.5초이며, 사이트 부하 방지를 위해 0.5초 미만으로 설정할 수 없습니다.
- 사이트 HTML 구조나 리뷰 말머리 번호가 바뀌면 선택자 또는 `REVIEW_HEAD_ID` 수정이 필요할 수 있습니다.
- 공개 게시물이더라도 작성자의 권리와 사이트 이용정책을 지키고, 개인 보관 용도로 사용하세요.
