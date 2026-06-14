"""전체 평가 500문항용 샘플 목록 생성 (seed=42).

출력: eval/llm_eval/full_sample.json
"""
import json
import random
from pathlib import Path

SEED = 42
TARGET_PER_CAT = 100
BASE = Path(__file__).parent.parent

def sample_lh(n: int) -> list[str]:
    files = sorted((BASE / 'data/lh_regulation/markdown').glob('*.md'))
    rng = random.Random(SEED)
    # 69개 파일에서 100개 → 일부 파일 2회 샘플링
    return [str(f) for f in rng.choices(files, k=n)]

def sample_kcsc(n: int) -> list[str]:
    """KDS/KCS/LHCS 비율 유지 층화 샘플."""
    rng = random.Random(SEED + 1)
    cache = BASE / 'data/kcsc/cache'
    kds = sorted(cache.glob('*_KDS*.json'))
    kcs = sorted(cache.glob('*_KCS*.json'))
    lhcs = sorted(cache.glob('*_LHCS*.json'))
    # 비율: KDS 530 / KCS 748 / LHCS 544 → 약 30/43/27%
    n_kds = round(n * 530 / (530 + 748 + 544))
    n_kcs = round(n * 748 / (530 + 748 + 544))
    n_lhcs = n - n_kds - n_kcs
    return (
        [str(f) for f in rng.sample(kds, min(n_kds, len(kds)))]
        + [str(f) for f in rng.sample(kcs, min(n_kcs, len(kcs)))]
        + [str(f) for f in rng.sample(lhcs, min(n_lhcs, len(lhcs)))]
    )[:n]

def sample_pps(n: int) -> list[str]:
    files = sorted((BASE / 'data/pps/cache').glob('*.json'))
    rng = random.Random(SEED + 2)
    return [str(f) for f in rng.sample(files, min(n, len(files)))]

# law/prec은 MCP 기반이므로 시드 키워드/법령 목록만 제공
LAW_SEEDS = [
    {'law': '주택법', 'topic': '사업계획승인', 'articles': '제15조~제17조'},
    {'law': '주택법', 'topic': '주택건설기준', 'articles': '제35조'},
    {'law': '공동주택관리법', 'topic': '하자담보책임', 'articles': '제36조~제38조'},
    {'law': '공동주택관리법', 'topic': '관리비 등', 'articles': '제23조'},
    {'law': '국가를 당사자로 하는 계약에 관한 법률', 'topic': '계약보증금', 'articles': '제12조'},
    {'law': '국가를 당사자로 하는 계약에 관한 법률', 'topic': '지체상금', 'articles': '제26조'},
    {'law': '건설산업기본법', 'topic': '하도급 제한', 'articles': '제29조'},
    {'law': '건설산업기본법', 'topic': '시공자격', 'articles': '제40조'},
    {'law': '공익사업을 위한 토지 등의 취득 및 보상에 관한 법률', 'topic': '이주대책', 'articles': '제78조'},
    {'law': '공익사업을 위한 토지 등의 취득 및 보상에 관한 법률', 'topic': '재결신청', 'articles': '제30조'},
    {'law': '건축법', 'topic': '건축허가', 'articles': '제11조'},
    {'law': '건축법', 'topic': '사용승인', 'articles': '제22조'},
    {'law': '도시 및 주거환경정비법', 'topic': '정비구역 지정', 'articles': '제16조'},
    {'law': '도시 및 주거환경정비법', 'topic': '관리처분계획', 'articles': '제74조'},
    {'law': '임대주택법', 'topic': '임대보증금 증액 제한', 'articles': '제7조'},
    {'law': '민간임대주택에 관한 특별법', 'topic': '등록임대사업자 의무', 'articles': '제43조'},
    {'law': '물가안정에 관한 법률', 'topic': '실거래가 신고', 'articles': '해당 조항'},
    {'law': '부동산 거래신고 등에 관한 법률', 'topic': '신고의무', 'articles': '제3조'},
    {'law': '택지개발촉진법', 'topic': '택지공급기준', 'articles': '제18조'},
    {'law': '산업입지 및 개발에 관한 법률', 'topic': '산업단지 지정', 'articles': '제6조'},
    {'law': '도시개발법', 'topic': '개발계획수립', 'articles': '제5조'},
    {'law': '빈집 및 소규모주택 정비에 관한 특례법', 'topic': '가로주택정비사업', 'articles': '제2조'},
    {'law': '공공주택 특별법', 'topic': '공공주택사업자 지정', 'articles': '제4조'},
    {'law': '공공주택 특별법', 'topic': '분양전환', 'articles': '제50조의2'},
    {'law': '주택도시기금법', 'topic': '기금 운용', 'articles': '제9조'},
]

PREC_SEEDS = [
    '지체상금', '공사대금', '수용재결', '임대차 보증금', '하자담보책임',
    '계약해제 손해배상', '장기계속공사', '낙찰자 결정', '설계변경',
    '이행보증금', '부정당업자 제재', '공사중지', '준공검사',
    '공사비 정산', '하도급 대금', '손실보상 재결', '수용보상금',
    '임대차 갱신거절', '전세권 말소', '토지수용 이의신청',
    '공동주택 하자보수', '재건축 매도청구', '분양계약 해제',
    '주택보증 구상권', '근저당권 말소',
]

def main():
    out = {
        'lh': sample_lh(TARGET_PER_CAT),
        'kcsc': sample_kcsc(TARGET_PER_CAT),
        'pps': sample_pps(TARGET_PER_CAT),
        'law_seeds': LAW_SEEDS[:TARGET_PER_CAT // 4],  # 25개 × 4문항 = 100
        'prec_seeds': PREC_SEEDS[:TARGET_PER_CAT // 3],  # 33개 × 3문항 ≈ 99+1
    }

    out_path = BASE / 'eval/llm_eval/full_sample.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"저장: {out_path}")
    for k, v in out.items():
        print(f"  {k}: {len(v)}개")

if __name__ == '__main__':
    main()
