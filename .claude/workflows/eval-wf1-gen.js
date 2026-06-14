export const meta = {
  name: 'eval-wf1-gen',
  description: '평가 WF-1: 5개 카테고리 병렬 질문 생성+검증 합친 단계',
  phases: [
    { title: 'GenValidate', detail: '카테고리별 원문 기반 생성+검증 동시 수행' },
  ],
}

// ─── 실행 모드 설정 ────────────────────────────────────────────────
// MODE: 'pilot' (5/cat) | 'full' (100/cat)
// SAMPLES 아래 full 경로 목록은 scripts/eval_sample_full.py로 생성 후 교체
const MODE = 'pilot'
const TARGET_PER_CAT = MODE === 'pilot' ? 5 : 100
const OVERSHOOT = Math.ceil(TARGET_PER_CAT * 1.1)  // 10% 여유분

// ─── 파일럿 샘플 (카테고리당 5개) ─────────────────────────────────
const SAMPLES = {
  lh: [
    '/Users/ldw/code/LH_RAG_MCP/code/data/lh_regulation/markdown/150306_복지후생규정시행세칙.md',
    '/Users/ldw/code/LH_RAG_MCP/code/data/lh_regulation/markdown/111031_해외근무직원의복무등에관한규정.md',
    '/Users/ldw/code/LH_RAG_MCP/code/data/lh_regulation/markdown/250813_여비규정.md',
    '/Users/ldw/code/LH_RAG_MCP/code/data/lh_regulation/markdown/250813_경영계획및관리에관한규정.md',
    '/Users/ldw/code/LH_RAG_MCP/code/data/lh_regulation/markdown/220902_임원추천위원회운영규정규정제270호.md',
  ],
  kcsc: [
    '/Users/ldw/code/LH_RAG_MCP/code/data/kcsc/cache/20190524_KDS474030.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/kcsc/cache/20180903_KCS612020.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/kcsc/cache/20201223_LHCS142041.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/kcsc/cache/20251230_KCS113015.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/kcsc/cache/20241212_KDS349915.json',
  ],
  pps: [
    '/Users/ldw/code/LH_RAG_MCP/code/data/pps/cache/20251204_429860.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/pps/cache/20251204_429764.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/pps/cache/20251204_431604.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/pps/cache/20251204_435582.json',
    '/Users/ldw/code/LH_RAG_MCP/code/data/pps/cache/20251204_435962.json',
  ],
}

// ─── 스키마 ────────────────────────────────────────────────────────
const GEN_VAL_SCHEMA = {
  type: 'object',
  properties: {
    confirmed: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          question: { type: 'string' },
          gold_answer: { type: 'string' },
          source_ref: { type: 'string' },
          source_quote: { type: 'string' },
        },
        required: ['id', 'question', 'gold_answer', 'source_ref', 'source_quote'],
      },
    },
    rejected: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          reason: { type: 'string' },
        },
        required: ['id', 'reason'],
      },
    },
  },
  required: ['confirmed', 'rejected'],
}

const COMMON_RULES = `
문항 작성 규칙 (전부 준수):
- LH(한국토지주택공사) 직원이 실무에서 실제로 물을 법한 자연어 질문.
- 대상 규정명/기준명/법령명을 질문 안에 자연스럽게 포함해 자립성 확보.
  예: "LH 여비규정상 국내 출장 일비는 얼마인가?"
- 정답이 원문 인용으로 객관적으로 검증 가능한 사실형 질문 (수치, 기간, 요건, 절차, 결론 등). 의견·해석 요구 금지.
- 질문 문장에 정답이 드러나면 안 된다.
- gold_answer: 2~3문장 이내 간결한 정답.
- source_ref: 문서명 + 조항/절 번호 (예: "여비규정 제12조", "KCS 61 20 20 3.2").
- source_quote: 정답을 뒷받침하는 원문 그대로의 인용 (300자 이내).

검증 기준 (하나라도 어기면 rejected에 포함):
1. 자립성: 질문만 보고 무엇을 묻는지 명확한가
2. 근거: gold_answer가 source_quote로 충분히 뒷받침되고, source_quote가 실제 원문에 존재하는가
3. 사실형: 정답이 객관적으로 채점 가능한가
4. 누설 없음: 질문 문장에 정답이 없는가
`

// ─── 카테고리 정의 ─────────────────────────────────────────────────
const JOBS = [
  {
    cat: 'lh',
    prompt: `다음 LH 규정 마크다운 파일 ${SAMPLES.lh.length}개를 각각 Read 도구로 읽어라:
${SAMPLES.lh.map((p, i) => `${i + 1}. ${p}`).join('\n')}

각 파일에서 1문항씩 + 예비 ${OVERSHOOT - TARGET_PER_CAT}문항(아무 파일), 총 ${OVERSHOOT}문항을 생성하라.
id는 lh-01 ~ lh-${String(OVERSHOOT).padStart(2,'0')}.

생성 즉시 검증도 함께 수행하라: 각 문항에 대해 Read로 원본 파일을 재확인해 source_quote가 실제 원문에 존재하는지 확인.
통과한 문항 → confirmed, 탈락한 문항 → rejected(reason 포함).
confirmed에서 최대 ${TARGET_PER_CAT}개만 반환하라.

${COMMON_RULES}`,
  },
  {
    cat: 'kcsc',
    prompt: `다음 국가건설기준(KDS/KCS/LHCS) JSON 파일 ${SAMPLES.kcsc.length}개를 각각 Read 도구로 읽어라:
${SAMPLES.kcsc.map((p, i) => `${i + 1}. ${p}`).join('\n')}

각 파일에서 1문항씩 + 예비 ${OVERSHOOT - TARGET_PER_CAT}문항, 총 ${OVERSHOOT}문항을 생성하라.
id는 kcsc-01 ~ kcsc-${String(OVERSHOOT).padStart(2,'0')}.
source_ref에는 기준 코드(예: KCS 61 20 20)와 절 번호를 포함하라.

생성 즉시 검증도 함께 수행: Read로 원본 JSON 파일을 재확인해 source_quote가 실제 본문에 존재하는지 확인(HTML 태그 차이 허용).
통과 → confirmed(최대 ${TARGET_PER_CAT}개), 탈락 → rejected(reason).

${COMMON_RULES}`,
  },
  {
    cat: 'pps',
    prompt: `다음 조달청 해석사례 JSON 파일 ${SAMPLES.pps.length}개를 각각 Read 도구로 읽어라:
${SAMPLES.pps.map((p, i) => `${i + 1}. ${p}`).join('\n')}

각 사례에서 1문항씩 + 예비 ${OVERSHOOT - TARGET_PER_CAT}문항, 총 ${OVERSHOOT}문항을 생성하라.
질의요지를 그대로 베끼지 말고 일반화된 실무 질문으로 재구성하되, 정답은 '회답'의 결론에서 가져와라.
id는 pps-01 ~ pps-${String(OVERSHOOT).padStart(2,'0')}.
source_ref는 "조달청 해석사례 {안건명 요약} (일련번호 {파일명의 숫자})" 형식.

생성 즉시 검증: Read로 원본 파일을 재확인해 gold_answer가 '회답'/'이유' 내용과 일치하는지 확인.
통과 → confirmed(최대 ${TARGET_PER_CAT}개), 탈락 → rejected(reason).

${COMMON_RULES}`,
  },
  {
    cat: 'law',
    prompt: `ToolSearch 도구에 query "search_law"를 넣어 LH RAG MCP 법령 검색 툴을 로드한 뒤 사용하라.

다음 5개 법령에서 지정 주제의 조문 원문을 조회해 각 1문항 + 예비 ${OVERSHOOT - TARGET_PER_CAT}문항, 총 ${OVERSHOOT}문항을 생성하라:
1. 주택법 — 사업계획승인 (제15조 부근)
2. 공동주택관리법 — 하자담보책임 (제36~37조 부근)
3. 국가를 당사자로 하는 계약에 관한 법률 — 계약보증금 (제12조 부근)
4. 건설산업기본법 — 하도급 제한 (제29조 부근)
5. 공익사업을 위한 토지 등의 취득 및 보상에 관한 법률 — 이주대책 (제78조 부근)

id는 law-01 ~ law-${String(OVERSHOOT).padStart(2,'0')}.
source_quote는 반드시 MCP 툴이 반환한 조문 원문에서 인용하라.
조회에 실패한 법령은 건너뛰고 성공한 법령에서 추가 생성하라.

검증: 동일 MCP 툴로 source_ref 조문을 재조회해 source_quote가 실제 조문과 일치하는지 확인.
통과 → confirmed(최대 ${TARGET_PER_CAT}개), 탈락 → rejected(reason).

${COMMON_RULES}`,
  },
  {
    cat: 'prec',
    prompt: `ToolSearch 도구에 query "search_precedents"를 넣어 LH RAG MCP 판례 검색 툴을 로드한 뒤 사용하라.

다음 5개 키워드로 각각 판례를 검색해 각 1문항 + 예비 ${OVERSHOOT - TARGET_PER_CAT}문항, 총 ${OVERSHOOT}문항을 생성하라:
1. "지체상금"
2. "공사대금"
3. "수용재결"
4. "임대차 보증금"
5. "하자담보책임"

id는 prec-01 ~ prec-${String(OVERSHOOT).padStart(2,'0')}.
질문은 "대법원 판례상 ~는 어떻게 판단되는가" 류, 사건번호는 질문에 넣지 마라.
gold_answer는 판결요지 결론. source_ref는 사건번호. source_quote는 판시사항/판결요지 원문.
검색 0건인 키워드는 건너뛰고 성공한 키워드에서 추가 생성하라.

검증: 동일 MCP 툴로 source_ref 사건번호가 실제 검색되는지, source_quote가 판결요지와 일치하는지 확인.
통과 → confirmed(최대 ${TARGET_PER_CAT}개), 탈락 → rejected(reason).

${COMMON_RULES}`,
  },
]

// ─── 실행 ─────────────────────────────────────────────────────────
phase('GenValidate')

const results = await parallel(
  JOBS.map((job) => () =>
    agent(
      `${job.prompt}\n\nconfirmed(검증 통과) 최대 ${TARGET_PER_CAT}개와 rejected(탈락) 목록을 반환하라.`,
      { label: `genval:${job.cat}`, phase: 'GenValidate', schema: GEN_VAL_SCHEMA },
    ).then((r) => ({ cat: job.cat, ...(r || { confirmed: [], rejected: [] }) }))
  )
)

const allConfirmed = []
const allRejected = []
for (const r of results.filter(Boolean)) {
  const ok = (r.confirmed || []).slice(0, TARGET_PER_CAT).map((q) => ({ ...q, category: r.cat }))
  const bad = (r.rejected || []).map((q) => ({ ...q, category: r.cat }))
  allConfirmed.push(...ok)
  allRejected.push(...bad)
  log(`${r.cat}: 확정 ${ok.length}개 / 탈락 ${bad.length}개`)
}

return { confirmed: allConfirmed, rejected: allRejected }
