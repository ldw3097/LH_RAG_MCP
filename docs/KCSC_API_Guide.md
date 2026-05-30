# 국가건설기준센터(KCSC) API 서비스 가이드

출처: [https://www.kcsc.re.kr/support/api](https://www.kcsc.re.kr/support/api)

## 1. API 기본 정보

| 항목 | 내용 |
| --- | --- |
| 서비스명 | KCSC Open API |
| 데이터 형식 | JSON |
| 인증 방식 | 인증키(API Key) |

## 2. 제공 기능 (Methods)

| Method | Request URL | 설명 |
| --- | --- | --- |
| GET | `https://kcsc.re.kr/OpenApi/CodeViewer` | 코드 상세 내용 조회 |
| GET | `https://kcsc.re.kr/OpenApi/CodeList` | 코드 목록 조회 |

## 3. 요청 변수 (Request Parameters)

| 요청 변수명 | 설명 | 예시 | Type | 필수 여부 |
| --- | --- | --- | --- | --- |
| **Type** | 문서 타입 | KDS | string | Y |
| **Code** | 문서 번호 | 101000 | string | Y |
| **Key** | 인증키 | bdf239cd... | string | Y |

## 4. 출력 결과 필드 (Output Fields)

| 필드명 | 설명 | Type |
| --- | --- | --- |
| **No** | 코드 고유번호 | int |
| **CodeType** | 코드 타입 | string |
| **Code** | 코드 번호 | string |
| **FullCode** | 카테고리 타입이 포함된 코드 번호 | string |
| **Name** | 코드 이름 | string |
| **Version** | 코드 버전 | string |
| **UpdateDate** | 코드 수정일자 | datetime |
| **Sort** | 목차 정렬 순서 번호 | int |
| **Title** | 목차 | string |
| **Contents** | 목차의 상세내용 | string |
| **List** | 특정코드 상세내용 리스트 | list |
| **ListParentCodes** | 카테고리 속성 리스트 | list |
| **Message** | 에러 내용 | string |

## 5. 예시
### CodeViewer API
- Request: `https://kcsc.re.kr/OpenApi/CodeViewer/KCS/114010?key={발급받은 키를, 중괄호 없이 이 위치에 입력하세요}`
- Response: 
```json
[
  {
    "no": 5783,
    "codeType": "KCS",
    "code": "114010",
    "fullCode": "2010114010",
    "name": "파형강판 암거",
    "version": "2025",
    "updateDate": "2025-12-30T16:28:40.63817",
    "list": [
      {
        "no": 5783,
        "sort": 1,
        "title": "1. 일반사항",
        "level": 1,
        "label": "1.",
        "contents": "<p>1. 일반사항</p>"
      }
    ]
  }
]
```
### CodeList API
- Request: `https://kcsc.re.kr/OpenApi/CodeList?key={발급받은 키를, 중괄호 없이 이 위치에 입력하세요}`
- Response: 
```json
[
  {
    "no": 4240,
    "codeType": "KDS",
    "code": "100000",
    "fullCode": "10101000",
    "name": "공통설계기준",
    "version": "2021",
    "updateDate": "2023-03-08T16:27:00",
    "list": null,
    "listParentCodes": [
      {
        "codeType": "KDS",
        "fullCode": "10",
        "name": "설계기준"
      }
    ],
    "message": null
  }
]
```
