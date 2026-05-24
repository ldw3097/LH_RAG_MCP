from contextvars import ContextVar

# 요청마다 사용자가 URL 파라미터로 전달하는 법제처 API 키
law_oc_var: ContextVar[str] = ContextVar("law_oc", default="")
