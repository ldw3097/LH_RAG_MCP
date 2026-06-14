export const meta = {
  name: 'eval-wf1-fill2',
  description: 'lh 4문항 추가 (기사용 섹션 제외, 다른 섹션에서 생성)',
  phases: [{ title: 'Gen', detail: 'lh 대형 파일 다른 섹션에서 4문항 생성' }],
}

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

const BASE = '/Users/ldw/code/LH_RAG_MCP/code/data/lh_regulation/markdown'

const FILES = [
  { path: BASE + '/250813_주택분양규정시행세칙.md',  exclude: '제26조(위약금)' },
  { path: BASE + '/150112_주택임대규정시행세칙.md',   exclude: '제9조(위약금)' },
  { path: BASE + '/210130_용지규정시행세칙.md',       exclude: '제13조(보상가격 산정)' },
  { path: BASE + '/260309_인사규정시행세칙.md',       exclude: '제25조의2(전보의 제한)' },
  { path: BASE + '/260112_개발사업규정시행세칙.md',   exclude: '제5조(사업후보지 조사)' },
  { path: BASE + '/260317_물품관리규정.md',           exclude: '제29조(불용품의 처분)' },
]

phase('Gen')

const result = await agent(
  `다음 6개 LH 규정 마크다운 파일을 Read 도구로 읽고,
파일당 1개 문항을 생성·검증하라. 단, 각 파일에서 아래 표시된 조항은 제외하고
다른 조항·섹션에서 문항을 생성해야 한다.

파일 목록 ([ 제외 조항 ]):
${FILES.map((f, i) => `  ${i+1}. ${f.path}\n     제외: ${f.exclude}`).join('\n')}

【문항 요건】
- LH 임직원 실무 사실형 질문 (수치·기간·요건·절차·한도)
- 원문 없이도 자립적으로 성립
- gold_answer: 원문에서 직접 확인 가능한 1~3문장 답
- source_ref: "파일명(확장자 제외) > 조항/섹션명"
- source_quote: 근거 원문 40~120자 (수치·기간 포함)

【불합격 기준】
- 모호·서술형 질문
- 제외 조항과 동일하거나 유사한 내용
- gold_answer를 source_quote로 검증 불가
- 수치·기간 없는 순수 개념형

id 형식: "fill2-lh-{01~06}"
category: "lh"
목표: 파일당 1개 confirmed (총 6개 중 최소 4개 이상)`,
  { label: 'fill2-lh', phase: 'Gen', schema: GEN_VAL_SCHEMA }
)

const confirmed = result ? result.confirmed : []
log(`생성: ${confirmed.length}개 confirmed`)
return { lh: confirmed }
