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
    async def search(self, query: str) -> list[SearchResult]:
        """키워드로 검색하여 관련 결과를 반환합니다."""
