export const meta = {
  name: 'eval-wf1-gen-full',
  description: '전체 평가 WF-1: 카테고리당 100문항 생성+검증 (500문항 목표)',
  phases: [
    { title: 'Load', detail: 'full_sample.json 로드' },
    { title: 'Gen', detail: '카테고리별 배치 병렬 생성+검증' },
  ],
}

const FULL_SAMPLE_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/full_sample.json'
const BATCH_FILES = 5   // lh/kcsc/pps: 파일 배치 크기
const SEEDS_PER_BATCH = 5  // law/prec: 시드 배치 크기 (× 4문항 = 20/배치)

const GEN_VAL_SCHEMA = {
  type: 'object',
  properties: {
    confirmed: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          category: { type: 'string' },
          question: { type: 'string' },
          gold_answer: { type: 'string' },
          source_ref: { type: 'string' },
          source_quote: { type: 'string' },
        },
        required: ['id', 'category', 'question', 'gold_answer', 'source_ref', 'source_quote'],
      },
    },
    rejected: {
      type: 'array',
      items: {
        type: 'object',
        properties: { id: { type: 'string' }, reason: { type: 'string' } },
        required: ['id', 'reason'],
      },
    },
  },
  required: ['confirmed', 'rejected'],
}

function chunk(arr, size) {
  const out = []
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size))
  return out
}

// ─── Phase 1: 샘플 목록 로드 ────────────────────────────────────────
phase('Load')
const sampleData = await agent(
  `Read 도구로 "${FULL_SAMPLE_PATH}" 파일을 읽어 JSON 내용을 그대로 반환하라.
  lh(배열), kcsc(배열), pps(배열), law_seeds(배열), prec_seeds(배열) 다섯 키가 필요하다.`,
  {
    label: 'load-sample',
    schema: {
      type: 'object',
      properties: {
        lh: { type: 'array', items: { type: 'string' } },
        kcsc: { type: 'array', items: { type: 'string' } },
        pps: { type: 'array', items: { type: 'string' } },
        law_seeds: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              law: { type: 'string' },
              topic: { type: 'string' },
              articles: { type: 'string' },
            },
            required: ['law', 'topic', 'articles'],
          },
        },
        prec_seeds: { type: 'array', items: { type: 'string' } },
      },
      required: ['lh', 'kcsc', 'pps', 'law_seeds', 'prec_seeds'],
    },
  }
)

if (!sampleData) throw new Error('full_sample.json 로드 실패')

const lhBatches   = chunk(sampleData.lh,         BATCH_FILES)
const kcscBatches = chunk(sampleData.kcsc,        BATCH_FILES)
const ppsBatches  = chunk(sampleData.pps,         BATCH_FILES)
const lawBatches  = chunk(sampleData.law_seeds,   SEEDS_PER_BATCH)
const precBatches = chunk(sampleData.prec_seeds,  SEEDS_PER_BATCH)

log(`배치 수: lh=${lhBatches.length}, kcsc=${kcscBatches.length}, pps=${ppsBatches.length}, law=${lawBatches.length}, prec=${precBatches.length} → 총 ${lhBatches.length+kcscBatches.length+ppsBatches.length+lawBatches.length+precBatches.length}개 에이전트`)

// ─── Phase 2: 생성+검증 ─────────────────────────────────────────────
phase('Gen')

const [lhAll, kcscAll, ppsAll, lawAll, precAll] = await parallel([

  // ── LH 규정 ──────────────────────────────────────────────────────
  () => pipeline(
    lhBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 LH 규정 마크다운 파일을 Read 도구로 순서대로 읽고,
파일당 1개 문항을 생성·검증해 반환하라.

파일 목록:
${batch.map((f, i) => `  ${i + 1}. ${f}`).join('\n')}

【문항 요건】
- LH 임직원이 실무에서 물을 법한 사실형 질문 (수치·기간·요건·절차·한도)
- 원문이 없어도 질문 자체가 자립적으로 성립
- gold_answer: 원문에서 직접 확인 가능한 1~3문장 답
- source_ref: "파일명(확장자 제외) > 조항명 또는 섹션명"
- source_quote: 정답 근거가 되는 원문 40~120자 (숫자·기간·요건 포함)

【불합격 기준】(→ rejected)
- "~에 대해 설명하라" 같은 모호·서술형 질문
- gold_answer를 source_quote로 검증할 수 없음
- 정답 수치·기간이 없는 순수 개념 설명형

id 형식: "lh-B${bIdx}-{01~${String(batch.length).padStart(2,'0')}}"
category: "lh"
목표: 파일당 1개 confirmed, 검증 실패 시 rejected`,
      { label: `lh:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // ── KCSC 건설기준 ────────────────────────────────────────────────
  () => pipeline(
    kcscBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 KCSC 건설기준 JSON 파일을 Read 도구로 읽고,
파일당 1개 문항을 생성·검증해 반환하라.

JSON 구조 예시: { "docId": "KCS612020", "docType": "KCS", "title": "...",
  "sections": [{ "sectionTitle": "...", "content": "..." }, ...] }

파일 목록:
${batch.map((f, i) => `  ${i + 1}. ${f}`).join('\n')}

【문항 요건】
- 건설·시공 실무자가 물을 법한 수치·기준·절차형 질문
- gold_answer: 원문의 수치·기준을 포함한 명확한 답
- source_ref: "문서코드(예: KDS 41 31 00) > 섹션명"
- source_quote: 근거 원문 40~120자 (수치·조건 포함)

【불합격 기준】
- 수치·기준이 없는 순수 개념형
- source_quote로 gold_answer 검증 불가

id 형식: "kcsc-B${bIdx}-{01~${String(batch.length).padStart(2,'0')}}"
category: "kcsc"
목표: 파일당 1개 confirmed`,
      { label: `kcsc:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // ── PPS 조달청 해석사례 ──────────────────────────────────────────
  () => pipeline(
    ppsBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 조달청 계약법규 해석사례 JSON 파일을 Read 도구로 읽고,
파일당 1개 문항을 생성·검증해 반환하라.

JSON 구조 예시: { "법령해석일련번호": "...", "안건명": "...",
  "질의요지": "...", "회답": "...", "이유": "...", "관련법령": "..." }

파일 목록:
${batch.map((f, i) => `  ${i + 1}. ${f}`).join('\n')}

【문항 요건】
- 질의요지를 자연어 질문으로 재구성 (원문 복사 금지)
- gold_answer: 회답의 핵심 1~2문장
- source_ref: "법령해석일련번호 > 안건명"
- source_quote: 회답 또는 이유 중 핵심 문장 40~120자

【불합격 기준】
- gold_answer가 "경우에 따라 다름" 등 비단정적
- source_quote로 검증 불가
- 질의요지 원문을 그대로 복사한 질문

id 형식: "pps-B${bIdx}-{01~${String(batch.length).padStart(2,'0')}}"
category: "pps"
목표: 파일당 1개 confirmed`,
      { label: `pps:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // ── LAW 법령 ─────────────────────────────────────────────────────
  () => pipeline(
    lawBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 법령 시드에 대해 MCP search_law 툴로 조문을 검색하고,
시드당 4개 문항을 생성·검증해 반환하라. 총 ${batch.length * 4}개 confirmed 목표.

【MCP 툴 사용법】
ToolSearch 도구로 "search_law"를 검색해 MCP 툴 스키마를 로드한 뒤 사용하라.
각 시드마다 법령명과 조항을 query/keywords로 검색해 실제 조문 원문을 확보하라.

법령 시드 목록:
${batch.map((s, i) => `  ${i + 1}. ${s.law} / ${s.topic} / ${s.articles}`).join('\n')}

【문항 요건 (시드당 4개)】
- 요건·기간·수치·절차·예외 등 각기 다른 측면
- gold_answer: 조문 원문 기반 명확한 답
- source_ref: "법령명 제X조 제Y항"
- source_quote: 해당 조문 40~120자

【불합격 기준】
- 조문에서 직접 확인 불가한 내용
- 비자립적 질문 (맥락 없이 이해 불가)
- 조문 원문 확보 실패 → 해당 시드 전체 rejected

id 형식: "law-B${bIdx}-{01~${String(batch.length * 4).padStart(2,'0')}}"
category: "law"`,
      { label: `law:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // ── PREC 판례 ────────────────────────────────────────────────────
  () => pipeline(
    precBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 키워드로 MCP search_precedents 툴로 판례를 검색하고,
키워드당 4개 문항을 생성·검증해 반환하라. 총 ${batch.length * 4}개 confirmed 목표.

【MCP 툴 사용법】
ToolSearch 도구로 "search_precedents"를 검색해 MCP 툴 스키마를 로드한 뒤 사용하라.
keywords 파라미터는 핵심 키워드 1~2개만 사용할 것 (AND 검색이므로 2개 초과 시 결과 없음).

키워드 목록:
${batch.map((k, i) => `  ${i + 1}. ${k}`).join('\n')}

【문항 요건 (키워드당 4개)】
- 판시사항·판결요지·법리·참조조문 각 측면
- gold_answer: 판결요지 기반 명확한 법리 답변
- source_ref: "사건번호 > 판시사항 또는 판결요지"
- source_quote: 판결요지 핵심 40~120자

【불합격 기준】
- 검색 결과 0건 → 해당 키워드 전체 rejected
- 판결요지에서 직접 확인 불가한 추론
- 비자립적·모호한 질문

id 형식: "prec-B${bIdx}-{01~${String(batch.length * 4).padStart(2,'0')}}"
category: "prec"`,
      { label: `prec:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),
])

// ─── 결과 취합 및 순번 재부여 ────────────────────────────────────────
function renumber(items, prefix) {
  return (items || []).filter(Boolean).map((item, i) => ({
    ...item,
    id: `${prefix}-${String(i + 1).padStart(3, '0')}`,
  }))
}

const lhQ   = renumber((lhAll   || []).flat(), 'lh')
const kcscQ = renumber((kcscAll || []).flat(), 'kcsc')
const ppsQ  = renumber((ppsAll  || []).flat(), 'pps')
const lawQ  = renumber((lawAll  || []).flat(), 'law')
const precQ = renumber((precAll || []).flat(), 'prec')

const allConfirmed = [...lhQ, ...kcscQ, ...ppsQ, ...lawQ, ...precQ]

log(`생성 완료 — lh: ${lhQ.length}, kcsc: ${kcscQ.length}, pps: ${ppsQ.length}, law: ${lawQ.length}, prec: ${precQ.length} → 합계: ${allConfirmed.length}개`)

return { confirmed: allConfirmed }
