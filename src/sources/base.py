from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    source_id: str
    title: str
    content: str
    url: str = ""
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def to_text(self) -> str:
        lines = [f"[{self.source_id}] {self.title}"]
        if self.url:
            lines.append(f"출처: {self.url}")
        lines.append(self.content)
        return "\n".join(lines)


class SearchSource(ABC):
    source_id: str

    @abstractmethod
    async def search(self, query: str, keywords: str) -> list[SearchResult]:
        """검색하여 관련 결과를 반환합니다.

        Args:
            query: 자연어 질의 (의미 기반 검색용 — AI검색·Dense 벡터검색).
            keywords: 공백 구분 핵심 키워드 (어휘 기반 검색용 — 일반검색·BM25·admrul).
        """

    async def aclose(self) -> None:
        """보유 리소스를 해제합니다. 필요한 서브클래스에서 오버라이드."""
