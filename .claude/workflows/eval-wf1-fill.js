export const meta = {
  name: 'eval-wf1-fill',
  description: '보완 WF-1: 42문항 갭 채우기 (lh+16, kcsc+10, pps+12, law+4)',
  phases: [
    { title: 'Load', detail: 'fill_sample.json 로드' },
    { title: 'Gen', detail: '카테고리별 배치 생성+검증' },
  ],
}

const FILL_SAMPLE_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/fill_sample.json'
const BATCH_FILES = 5

const GEN_VAL_SCHEMA = {
  type: 'object',
  properties: {
    confirmed: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id:           { type: 'string' },
          category:     { type: 'string' },
          question:     { type: 'string' },
          gold_answer:  { type: 'string' },
          source_ref:   { type: 'string' },
          source_quote: { type: 'string' },
        },
        required: ['id','category','question','gold_answer','source_ref','source_quote'],
      },
    },
    rejected: {
      type: 'array',
      items: {
        type: 'object',
        properties: { id: { type: 'string' }, reason: { type: 'string' } },
        required: ['id','reason'],
      },
    },
  },
  required: ['confirmed','rejected'],
}

function chunk(arr, size) {
  const out = []
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size))
  return out
}

// ─── Load ────────────────────────────────────────────────────────────
phase('Load')
const sample = await agent(
  `Read 도구로 "${FILL_SAMPLE_PATH}" 파일을 읽어 JSON 내용을 반환하라.`,
  {
    label: 'load-fill',
    schema: {
      type: 'object',
      properties: {
        lh:        { type: 'array', items: { type: 'string' } },
        kcsc:      { type: 'array', items: { type: 'string' } },
        pps:       { type: 'array', items: { type: 'string' } },
        law_seeds: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              law:      { type: 'string' },
              topic:    { type: 'string' },
              articles: { type: 'string' },
            },
            required: ['law','topic','articles'],
          },
        },
      },
      required: ['lh','kcsc','pps','law_seeds'],
    },
  }
)
if (!sample) throw new Error('fill_sample.json 로드 실패')

const lhBatches   = chunk(sample.lh,        BATCH_FILES)
const kcscBatches = chunk(sample.kcsc,       BATCH_FILES)
const ppsBatches  = chunk(sample.pps,        BATCH_FILES)
log(`배치 수: lh=${lhBatches.length}, kcsc=${kcscBatches.length}, pps=${ppsBatches.length}, law=1`)

// ─── Gen ─────────────────────────────────────────────────────────────
phase('Gen')

const [lhAll, kcscAll, ppsAll, lawResult] = await parallel([

  // LH 규정
  () => pipeline(
    lhBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 LH 규정 마크다운 파일을 Read 도구로 읽고,
파일당 1개 문항을 생성·검증해 반환하라.

파일 목록:
${batch.map((f, i) => `  ${i + 1}. ${f}`).join('\n')}

【문항 요건】
- LH 임직원 실무 사실형 질문 (수치·기간·요건·절차·한도)
- 원문 없이도 자립적으로 성립
- gold_answer: 원문에서 직접 확인 가능한 1~3문장 답
- source_ref: "파일명(확장자 제외) > 조항/섹션명"
- source_quote: 근거 원문 40~120자 (수치·기간 포함)

【불합격 기준】
- 모호·서술형 질문
- gold_answer를 source_quote로 검증 불가
- 수치·기간 없는 순수 개념형

id 형식: "fill-lh-B${bIdx}-{01~${String(batch.length).padStart(2,'0')}}"
category: "lh"
목표: 파일당 1개 confirmed`,
      { label: `fill-lh:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // KCSC 건설기준
  () => pipeline(
    kcscBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 KCSC 건설기준 JSON 파일을 Read 도구로 읽고,
파일당 1개 문항을 생성·검증해 반환하라.

JSON 구조: { "docId": "...", "docType": "KDS|KCS|LHCS", "title": "...",
  "sections": [{ "sectionTitle": "...", "content": "..." }] }

파일 목록:
${batch.map((f, i) => `  ${i + 1}. ${f}`).join('\n')}

【문항 요건】
- 건설 실무자가 물을 법한 수치·기준·절차형 질문
- gold_answer: 원문 수치·기준 포함한 명확한 답
- source_ref: "문서코드 > 섹션명"
- source_quote: 근거 원문 40~120자

【불합격 기준】
- 수치·기준 없는 개념형
- source_quote로 검증 불가

id 형식: "fill-kcsc-B${bIdx}-{01~${String(batch.length).padStart(2,'0')}}"
category: "kcsc"
목표: 파일당 1개 confirmed`,
      { label: `fill-kcsc:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // PPS 조달청 해석사례
  () => pipeline(
    ppsBatches,
    (batch, _, bIdx) => agent(
      `다음 ${batch.length}개 조달청 해석사례 JSON 파일을 Read 도구로 읽고,
파일당 1개 문항을 생성·검증해 반환하라.

JSON 구조: { "법령해석일련번호": "...", "안건명": "...",
  "질의요지": "...", "회답": "...", "이유": "...", "관련법령": "..." }

파일 목록:
${batch.map((f, i) => `  ${i + 1}. ${f}`).join('\n')}

【문항 요건】
- 질의요지를 자연어 질문으로 재구성 (원문 복사 금지)
- gold_answer: 회답 핵심 1~2문장
- source_ref: "법령해석일련번호 > 안건명"
- source_quote: 회답/이유 핵심 40~120자

【불합격 기준】
- gold_answer가 비단정적
- 질의요지 원문 그대로 복사
- source_quote로 검증 불가

id 형식: "fill-pps-B${bIdx}-{01~${String(batch.length).padStart(2,'0')}}"
category: "pps"
목표: 파일당 1개 confirmed`,
      { label: `fill-pps:B${bIdx}`, phase: 'Gen', schema: GEN_VAL_SCHEMA }
    ).then(r => r ? r.confirmed : [])
  ),

  // LAW 법령 (새 시드 2개 × 4문항 = 8개, 그 중 필요한 만큼 사용)
  () => agent(
    `다음 2개 법령 시드에 대해 MCP search_law 툴로 조문을 검색하고,
시드당 4개 문항을 생성·검증해 반환하라. 총 8개 confirmed 목표.

ToolSearch 도구로 "search_law"를 검색해 MCP 툴을 로드한 뒤 사용하라.

법령 시드:
  1. ${sample.law_seeds[0] ? `${sample.law_seeds[0].law} / ${sample.law_seeds[0].topic} / ${sample.law_seeds[0].articles}` : '공공주택 특별법 / 임대차계약 해제·해지 / 제49조의3'}
  2. ${sample.law_seeds[1] ? `${sample.law_seeds[1].law} / ${sample.law_seeds[1].topic} / ${sample.law_seeds[1].articles}` : '도시개발법 / 수용·사용 방식 / 제22조~제24조'}

【문항 요건 (시드당 4개)】
- 요건·기간·수치·절차 각기 다른 측면
- gold_answer: 조문 원문 기반 명확한 답
- source_ref: "법령명 제X조 제Y항"
- source_quote: 해당 조문 40~120자

id 형식: "fill-law-{01~08}"
category: "law"`,
    { label: 'fill-law', phase: 'Gen', schema: GEN_VAL_SCHEMA }
  ).then(r => r ? r.confirmed : []),
])

// ─── 결과 반환 (id는 Python에서 재부여) ────────────────────────────
const lhQ   = (lhAll   || []).flat().filter(Boolean)
const kcscQ = (kcscAll || []).flat().filter(Boolean)
const ppsQ  = (ppsAll  || []).flat().filter(Boolean)
const lawQ  = (lawResult || []).filter(Boolean)

log(`보완 생성: lh=${lhQ.length}, kcsc=${kcscQ.length}, pps=${ppsQ.length}, law=${lawQ.length} → 합계=${lhQ.length+kcscQ.length+ppsQ.length+lawQ.length}개`)

return { lh: lhQ, kcsc: kcscQ, pps: ppsQ, law: lawQ }
