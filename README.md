# Simple S3 Gateway

로컬 디렉터리를 **읽기 전용 S3 호환 엔드포인트**로 노출하는 단일 파일 Java 서버(`S3GetObjectServer.java`).
`aws-cli`, `boto3` 등 S3 클라이언트로 로컬 파일을 `GetObject` / `HeadObject` / `ListObjectsV2` 할 수 있다.

- Java 8 이상, 외부 의존성 없음 (JDK 내장 `com.sun.net.httpserver` 사용)
- path-style 주소(`http://host:port/{bucket}/{key}`)만 지원
- 인증 없음 (SigV4 서명을 검증하지 않음)

## 배경

이 프로젝트는 다음과 같은 상황에서 출발했다.

- 운영 환경의 런타임이 **Java 8** 로 고정되어 있어, 최신 JDK를 요구하는 도구나 MinIO 같은 별도 서버 제품을 들이기 어렵다.
- **Object Storage(S3, Ceph, MinIO 등)가 없다.** 데이터는 서버의 로컬 디스크(또는 NFS 마운트)에 일반 파일로만 존재한다.
- 그런데 사용하는 애플리케이션·도구는 **S3 호환 API** 로만 데이터를 읽을 수 있다.

### 예시

배치 서버의 `/data/s3/reports/2026/10/sales.csv` 에 매일 생성되는 리포트 파일이 있다고 하자.
분석 도구와 Python 스크립트(boto3)는 데이터 소스를 `s3://reports/2026/10/sales.csv` 형태로만 받는다.
Object Storage를 새로 도입하고 파일을 업로드·동기화하는 파이프라인을 만드는 것은 이 목적에 비해 과하다.

이 게이트웨이를 쓰면 파일을 옮기거나 복제하지 않고 같은 디스크를 그대로 S3 API로 노출할 수 있다.

```
/data/s3/                            S3 API
└── reports/             ──────▶     버킷  reports
    └── 2026/10/sales.csv            키    2026/10/sales.csv
```

```bash
java S3GetObjectServer 9000 /data/s3
aws --endpoint-url http://localhost:9000 s3 cp s3://reports/2026/10/sales.csv .
```

기존 도구는 `endpoint_url` 만 바꾸면 되고, 파일은 계속 평소처럼 디스크에 쓰면 된다.
읽기 전용(GET/HEAD/ListObjectsV2)만 필요하다는 점을 이용해, 구현을 JDK 내장 `HttpServer` 위의 **단일 Java 파일** 로 최소화했다. 빌드 도구나 라이브러리 없이 `javac` 한 번이면 Java 8 환경에서 바로 동작한다.

## 디렉터리 구조

```
.
├── S3GetObjectServer.java   # 서버 구현 (단일 파일)
├── data/                    # 예시 데이터 루트 (하위 디렉터리 = 버킷)
│   └── bucket1/test1.txt
└── tests/                   # 블랙박스 테스트 (자세한 내용은 tests/README.md)
```

## 빠른 시작

```bash
javac S3GetObjectServer.java
java S3GetObjectServer [port] [root]
# 예: java S3GetObjectServer 9000 data
```

| 인자 | 기본값 | 설명 |
|---|---|---|
| `port` | `9000` | 리슨 포트 |
| `root` | `/data/s3` | 데이터 루트. **바로 아래 디렉터리 하나가 버킷 하나**, 그 안의 파일 경로가 객체 키 |

예를 들어 `data/bucket1/docs/a.txt` 는 버킷 `bucket1`, 키 `docs/a.txt` 가 된다.

## 지원 API

| 요청 | 동작 |
|---|---|
| `GET /{bucket}/{key}` | GetObject. 단일 `Range` 지원 (`bytes=a-b`, `a-`, `-n`) → `206` |
| `HEAD /{bucket}/{key}` | HeadObject (`Content-Length`, `Last-Modified`, `Accept-Ranges`) |
| `GET /{bucket}?list-type=2` | ListObjectsV2: `prefix`, `delimiter`, `max-keys`(최대 1000), `continuation-token` |
| `PUT` / `POST` / `DELETE` 등 | `405 MethodNotAllowed` (`Allow: GET, HEAD`) |
| ListObjects v1 (`list-type` 없음/1) | `501 NotImplemented` |

오류는 S3 형식 XML(`<Error><Code>…</Code><Message>…</Message></Error>`)로 응답한다.

| 상태 | 코드 | 상황 |
|---|---|---|
| 400 | `InvalidURI`, `InvalidBucketName`, `InvalidArgument` | 잘못된 경로/버킷명/`max-keys`/토큰 |
| 403 | `AccessDenied` | 키에 `\` 포함 |
| 404 | `NoSuchBucket`, `NoSuchKey` | 버킷 또는 객체 없음 (디렉터리 키 포함) |
| 405 | `MethodNotAllowed` | GET/HEAD 외 메서드 |
| 416 | `InvalidRange` | 만족할 수 없거나 잘못된 Range (다중 범위 포함) |
| 501 | `NotImplemented` | ListObjects v1 |
| 500 | `InternalError` | 처리 중 예외 |

## 사용 예

```bash
# aws-cli
aws --endpoint-url http://localhost:9000 s3 ls s3://bucket1/
aws --endpoint-url http://localhost:9000 s3 cp s3://bucket1/test1.txt .

# curl
curl http://localhost:9000/bucket1/test1.txt
curl -r 0-9 http://localhost:9000/bucket1/test1.txt
curl "http://localhost:9000/bucket1?list-type=2&prefix=docs/&delimiter=/"
```

```python
import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",
    region_name="us-east-1",
    aws_access_key_id="x", aws_secret_access_key="y",   # 아무 값이나 가능
    config=Config(s3={"addressing_style": "path"}),     # 필수
)
print(s3.list_objects_v2(Bucket="bucket1", Delimiter="/"))
```

`delimiter="/"` 를 쓰면 하위 폴더는 `CommonPrefixes`, 파일은 `Contents` 로 반환된다 (실제 폴더 객체는 없음).

## 동작 세부사항

- **경로 보안**: URL 디코딩 후 정규화하여 루트/버킷 밖으로 벗어나는 경로(`..`), 버킷명 `.`/`..`/`\`, 키의 `\` 를 거부한다. 심볼릭 링크는 실제 경로가 버킷 밖이면 `404` 처리한다.
- **목록 페이지네이션**: 키 정렬 순서의 오프셋을 Base64(URL-safe)로 인코딩한 값을 `NextContinuationToken` 으로 사용한다. 목록은 요청마다 디렉터리를 다시 순회하므로, 요청 사이에 파일이 바뀌면 페이지 경계가 어긋날 수 있다.
- **ETag 미제공**, `StorageClass` 는 항상 `STANDARD`.
- **스레딩**: 고정 16 스레드 풀.
- **로그**: 요청마다 stdout 에 한 줄 출력한다.
  `<시각> client=<IP> method=<M> uri="<URI>" status=<코드> bytes=<전송량> duration_ms=<ms>`
- **Content-Type**: `Files.probeContentType` 결과를 사용하며, 알 수 없으면 `application/octet-stream`.

## 제약 사항

- 읽기 전용: 업로드·삭제·멀티파트 등 쓰기 API 없음
- virtual-host 스타일 주소 미지원
- 인증·권한·TLS 없음 → 신뢰할 수 있는 네트워크에서만 사용하거나 리버스 프록시 뒤에 둘 것
- 다중 Range, 조건부 요청(`If-*`), 버전 관리, 사용자 메타데이터 미지원
- 잘못된 퍼센트 인코딩(`/bucket/%zz`)은 JDK `HttpServer` 가 서버 코드 이전에 HTML 본문의 `400` 으로 거절한다

## 테스트

서버를 띄운 뒤 `tests/` 의 Python 테스트를 실행한다 (HTTP 레벨 테스트 + boto3 호환성 테스트).

```bash
java S3GetObjectServer 9020 data &
cd tests
python3 -m unittest discover -v
```

환경 변수, boto3 설치 방법 등은 [tests/README.md](tests/README.md) 참고.
