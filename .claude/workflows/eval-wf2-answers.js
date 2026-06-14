export const meta = {
  name: 'eval-wf2-answers',
  description: '평가 WF-2: 5개 카테고리 병렬, 배치 10문항/에이전트 답변 수집',
  phases: [
    { title: 'CondA', detail: '웹검색만 허용, MCP 없음' },
    { title: 'CondB', detail: '웹+MCP 툴 사용' },
  ],
}

// ─── 문항 목록 (메인 루프가 교체) ─────────────────────────────────
// 이 파일은 Workflow scriptPath로 호출되므로 args로 questions를 받는다.
// args = [{ id, category, question, gold_answer, source_ref, source_quote }, ...]
// args가 없으면 eval/llm_eval/questions.jsonl을 Read하는 방식으로 에이전트가 로드함.
const QUESTIONS_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/pilot2_questions.jsonl'
const BATCH_SIZE = 10

const ANSWER_SCHEMA = {
  type: 'object',
  properties: {
    answers: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          qid: { type: 'string' },
          condition: { type: 'string' },
          answer: { type: 'string' },
          tools_used: { type: 'array', items: { type: 'string' } },
        },
        required: ['qid', 'condition', 'answer', 'tools_used'],
      },
    },
  },
  required: ['answers'],
}

// ─── questions.jsonl 로드 에이전트 ────────────────────────────────
phase('CondA')
log('questions.jsonl 로드 중...')

const loadResult = await agent(
  `Read 도구로 "${QUESTIONS_PATH}" 파일을 읽어라.
각 줄은 JSON 객체다. 모든 문항의 id, category, question을 배열로 반환하라.`,
  {
    label: 'load-questions',
    schema: {
      type: 'object',
      properties: {
        questions: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              id: { type: 'string' },
              category: { type: 'string' },
              question: { type: 'string' },
            },
            required: ['id', 'category', 'question'],
          },
        },
      },
      required: ['questions'],
    },
  }
)

const questions = (loadResult && loadResult.questions) ? loadResult.questions : []
log(`총 ${questions.length}개 문항 로드됨`)

// 카테고리별로 그룹화 후 배치 분할
const byCategory = {}
for (const q of questions) {
  if (!byCategory[q.category]) byCategory[q.category] = []
  byCategory[q.category].push(q)
}

// 배치 목록 생성 (카테고리 × 배치)
const batches = []
for (const [cat, qs] of Object.entries(byCategory)) {
  for (let i = 0; i < qs.length; i += BATCH_SIZE) {
    batches.push({ cat, batch: qs.slice(i, i + BATCH_SIZE), batchIdx: Math.floor(i / BATCH_SIZE) })
  }
}

log(`${batches.length}개 배치 생성 (카테고리별 ${BATCH_SIZE}문항씩)`)

// ─── 조건 A: 웹검색만, MCP 금지 ──────────────────────────────────
const condAResults = await parallel(
  batches.map((b) => () =>
    agent(
      `다음 ${b.batch.length}개 질문에 각각 답하라. 카테고리: ${b.cat}

【규칙 — 반드시 준수】
- WebSearch와 WebFetch 도구만 사용 가능하다.
- 이 저장소의 data/ 또는 eval/ 디렉터리 파일을 Read하지 말 것.
- ToolSearch 또는 MCP 툴을 사용하지 말 것.
- 웹에서 찾은 정보를 바탕으로 최대한 정확하게 답하라.
- 웹에서 찾지 못하면 솔직하게 "웹 검색으로 확인 불가" 라고 명시하라.

질문 목록:
${b.batch.map((q) => `[${q.id}] ${q.question}`).join('\n')}

각 질문에 대해 qid, condition="A", answer(2~4문장), tools_used(사용한 도구 이름 목록) 반환.`,
      { label: `condA:${b.cat}:${b.batchIdx}`, phase: 'CondA', schema: ANSWER_SCHEMA }
    ).then((r) => r ? r.answers : [])
  )
)

const condA = condAResults.filter(Boolean).flat()
log(`조건A: ${condA.length}개 답변 수집`)

// ─── 조건 B: 웹+MCP ───────────────────────────────────────────────
phase('CondB')

const MCP_TOOL_HINTS = {
  lh: 'ToolSearch query "search_lh_regulations" → LH 규정 검색 MCP 툴 로드',
  kcsc: 'ToolSearch query "search_construction_standards" → 건설기준 검색 MCP 툴 로드',
  pps: 'ToolSearch query "search_procurement_interpretations" → 조달청 해석사례 검색 MCP 툴 로드',
  law: 'ToolSearch query "search_law" → 법령 검색 MCP 툴 로드',
  prec: 'ToolSearch query "search_precedents" → 판례 검색 MCP 툴 로드',
}

const condBResults = await parallel(
  batches.map((b) => () =>
    agent(
      `다음 ${b.batch.length}개 질문에 각각 답하라. 카테고리: ${b.cat}

【규칙】
- 먼저 ${MCP_TOOL_HINTS[b.cat] || 'ToolSearch로 관련 MCP 툴 로드'}하여 우선 사용하라.
- MCP 결과를 우선 활용하고, 필요시 WebSearch/WebFetch로 보완하라.
- MCP 툴로 답이 충분히 나오면 웹 검색 불필요.

질문 목록:
${b.batch.map((q) => `[${q.id}] ${q.question}`).join('\n')}

각 질문에 대해 qid, condition="B", answer(2~4문장), tools_used(사용한 도구 이름 목록) 반환.`,
      { label: `condB:${b.cat}:${b.batchIdx}`, phase: 'CondB', schema: ANSWER_SCHEMA }
    ).then((r) => r ? r.answers : [])
  )
)

const condB = condBResults.filter(Boolean).flat()
log(`조건B: ${condB.length}개 답변 수집`)

return { condA, condB }
