# S3 Gateway 테스트

`S3GetObjectServer`(읽기 전용, path-style S3 호환 게이트웨이)의 블랙박스 테스트.

## 지원 범위 (서버 기준)

| 기능 | 동작 |
|---|---|
| `GET /{bucket}/{key}` | GetObject (Range 지원) |
| `HEAD /{bucket}/{key}` | HeadObject |
| `GET /{bucket}?list-type=2` | ListObjectsV2 (`prefix`, `delimiter`, `max-keys`, `continuation-token`) |
| 그 외 메서드 (PUT/POST/DELETE) | `405 MethodNotAllowed` |
| ListObjects v1 (`list-type` 없음/1) | `501 NotImplemented` |
| 인증 | 없음 (서명 검증 안 함) |

## 파일

| 파일 | 내용 | 의존성 |
|---|---|---|
| `test_s3_gateway.py` | HTTP 레벨 테스트 55개: 객체 조회, Range, 목록, 오류 응답, 경로 보안, 동시성 | 표준 라이브러리만 |
| `test_boto3.py` | boto3 호환성 17개: `list_objects_v2`, paginator, `get_object`(Range), `head_object`, `download_file`, 오류 코드 | `boto3` (없으면 skip) |
| `requirements.txt` | boto3 설치용 | |

## 실행

1. 서버 실행 (예: 포트 9020, 데이터 루트 `../data`)
   ```
   cd .. && javac S3GetObjectServer.java && java S3GetObjectServer 9020 data
   ```
2. 테스트 실행
   ```
   cd tests
   python3 -m unittest -v test_s3_gateway          # 표준 라이브러리만
   python3 -m unittest -v test_boto3               # boto3 필요
   python3 -m unittest discover -v                 # 전체
   ```

boto3 설치 (시스템 Python에 pip가 없다면 venv 사용):
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
```

### 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `S3_ENDPOINT` | `http://localhost:9020` | 테스트 대상 서버 |
| `S3_DATA_DIR` | `../data` (tests 기준) | **서버의 데이터 루트와 같은 디렉터리** |

## 동작 방식

- 테스트는 `S3_DATA_DIR` 아래에 임의 이름의 임시 버킷(`test-xxxx`, `boto-xxxx`)을 만들어 픽스처를 쓰고, 종료 시 삭제한다. 기존 버킷(`bucket1` 등)은 건드리지 않는다.
- 서버에 접속할 수 없거나 `S3_DATA_DIR`가 없으면 테스트는 실패가 아니라 **skip** 된다.
- 서버와 테스트가 같은 파일시스템을 공유해야 한다 (원격 서버 대상이면 사용 불가).

## boto3 사용 시 주의

```python
import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:9020",
    region_name="us-east-1",
    aws_access_key_id="x", aws_secret_access_key="y",   # 아무 값이나 가능
    config=Config(s3={"addressing_style": "path"}),     # 필수: virtual-host 미지원
)
resp = s3.list_objects_v2(Bucket="bucket1", Delimiter="/")
```

- `addressing_style="path"` 필수 (서버가 path-style만 처리).
- `delimiter="/"` 사용 시 폴더는 `CommonPrefixes`, 파일은 `Contents`로 반환된다. 실제 폴더 객체는 없다.

## 알려진 제약 / 참고

- 잘못된 퍼센트 인코딩(`/bucket/%zz`)은 JDK `HttpServer`가 서버 코드 이전에 HTML 본문의 400으로 거절한다. S3 형식 XML 오류가 아니다.
- 서명(SigV4)은 검증하지 않으므로 인증 관련 테스트는 없다.
- 요청 헤더 `Content-Type`은 `Files.probeContentType`에 의존해 OS/JDK에 따라 다를 수 있어 값 자체는 검증하지 않는다.
