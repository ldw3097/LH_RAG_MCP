"""fly.io 배포 서버 전 툴 E2E 테스트.

MCP Streamable-HTTP (JSON-RPC 2.0) 프로토콜로 서버를 직접 호출한다.
흐름: initialize → tools/list → tools/call (각 툴)
"""

import asyncio
import json
import sys
import time

import httpx

SERVER_URL = "https://lh-rag-mcp.fly.dev/mcp"
LAW_OC = "we-407bt"
TIMEOUT = 60

DIVIDER = "=" * 72
SUB_DIV = "-" * 72

# ──────────────────────────────────────────────
# 테스트 케이스: (툴 이름, 인자, 설명)
# ──────────────────────────────────────────────
TEST_CASES = [
    (
        "search_law",
        {"query": "공공주택 분양가 산정 기준", "keywords": "분양가 공공주택"},
        "법령 검색 — 공공주택 분양가",
    ),
    (
        "search_lh_regulations",
        {"query": "임대주택 입주자격 소득 기준", "keywords": "임대 입주자격 소득"},
        "LH 규정 검색 — 임대주택 입주자격",
    ),
    (
        "search_construction_standards",
        {"query": "콘크리트 압축강도 시험방법 및 기준", "keywords": "압축강도 시험"},
        "KCSC 건설기준 검색 — 콘크리트 압축강도",
    ),
    (
        "search_precedents",
        {"keywords": "분양가 상한제 위헌"},
        "판례 검색 — 분양가 상한제",
    ),
]


async def mcp_call(client: httpx.AsyncClient, tool: str, args: dict, session_id: str | None = None) -> dict:
    """MCP tools/call 요청."""
    url = f"{SERVER_URL}?law_oc={LAW_OC}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    resp = await client.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()

    # Streamable-HTTP: SSE 또는 JSON 응답 처리
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        # SSE 에서 data: 라인 추출
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    return json.loads(data)
        return {"error": "SSE 응답에 data 없음"}
    return resp.json()


async def mcp_initialize(client: httpx.AsyncClient) -> tuple[dict, str | None]:
    """initialize 요청으로 서버 버전 확인. 세션 ID 반환."""
    url = f"{SERVER_URL}?law_oc={LAW_OC}"
    payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "1.0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    resp = await client.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()

    session_id = resp.headers.get("mcp-session-id")

    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    return json.loads(data), session_id
        return {}, session_id
    return resp.json(), session_id


def extract_text(result: dict) -> str:
    """MCP tools/call 결과에서 텍스트 추출."""
    if "error" in result:
        return f"[ERROR] {result['error']}"
    content = result.get("result", {}).get("content", [])
    if not content:
        return "(응답 없음)"
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    return "\n".join(texts)


def truncate(text: str, max_chars: int = 600) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… (이하 {len(text)-max_chars}자 생략)"


async def run():
    print(DIVIDER)
    print(f"fly.io E2E 테스트  →  {SERVER_URL}")
    print(DIVIDER)

    async with httpx.AsyncClient() as client:
        # 1. initialize
        print("[ initialize ]")
        t0 = time.perf_counter()
        try:
            init, session_id = await mcp_initialize(client)
            si = init.get("result", {}).get("serverInfo", {})
            caps = init.get("result", {}).get("capabilities", {})
            print(f"  서버: {si.get('name','?')} v{si.get('version','?')}")
            print(f"  session_id: {session_id or '없음'}")
            print(f"  capabilities: {list(caps.keys())}")
            print(f"  ({time.perf_counter()-t0:.2f}s)")
        except Exception as e:
            print(f"  initialize 실패: {e}")
            sys.exit(1)
        print()

        # 2. 각 툴 호출
        pass_count = 0
        for tool, args, desc in TEST_CASES:
            print(DIVIDER)
            print(f"툴: {tool}")
            print(f"설명: {desc}")
            print(f"인자: {json.dumps(args, ensure_ascii=False)}")
            print(SUB_DIV)

            t0 = time.perf_counter()
            try:
                result = await mcp_call(client, tool, args, session_id)
                elapsed = time.perf_counter() - t0
                text = extract_text(result)

                if "[ERROR]" in text or not text.strip() or text == "(응답 없음)":
                    status = "✗ FAIL"
                else:
                    status = "✓ PASS"
                    pass_count += 1

                print(f"  상태: {status}  ({elapsed:.2f}s)")
                print(f"  응답 길이: {len(text)}자")
                print()
                for line in truncate(text, 600).splitlines():
                    print(f"  {line}")
            except Exception as e:
                elapsed = time.perf_counter() - t0
                print(f"  상태: ✗ FAIL  ({elapsed:.2f}s)")
                print(f"  예외: {e}")
            print()

    print(DIVIDER)
    print("테스트 요약")
    print(DIVIDER)
    print(f"  총 케이스: {len(TEST_CASES)}건")
    print(f"  PASS    : {pass_count}건")
    print(f"  FAIL    : {len(TEST_CASES)-pass_count}건")
    if pass_count == len(TEST_CASES):
        print("\n  ✓ 전 케이스 PASS")
    else:
        print(f"\n  ✗ {len(TEST_CASES)-pass_count}건 실패")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(run())
