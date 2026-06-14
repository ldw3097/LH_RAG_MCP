export const meta = {
  name: 'eval-wf3-judge',
  description: '평가 WF-3: 블라인드 채점, 배치 20건/에이전트',
  phases: [
    { title: 'Judge', detail: '정답/부분정답/오답 블라인드 채점' },
  ],
}

const JUDGE_BATCH_SIZE = 20
const ANSWERS_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/pilot2_answers.jsonl'
const QUESTIONS_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/pilot2_questions.jsonl'

const JUDGE_SCHEMA = {
  type: 'object',
  properties: {
    judgments: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          qid: { type: 'string' },
          condition: { type: 'string' },
          verdict: { type: 'string', enum: ['정답', '부분정답', '오답'] },
          reason: { type: 'string' },
        },
        required: ['qid', 'condition', 'verdict', 'reason'],
      },
    },
  },
  required: ['judgments'],
}

// ─── 데이터 로드 ───────────────────────────────────────────────────
phase('Judge')
log('questions.jsonl + answers.jsonl 로드 중...')

const loadResult = await agent(
  `Read 도구로 두 파일을 읽어라:
1. "${QUESTIONS_PATH}"
2. "${ANSWERS_PATH}"

두 파일 모두 줄당 JSON 객체 형식이다.
questions: id, question, gold_answer, source_quote 필드 추출.
answers: qid, condition, answer 필드 추출.
반환 형식: { questions: [...], answers: [...] }`,
  {
    label: 'load-data',
    schema: {
      type: 'object',
      properties: {
        questions: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              id: { type: 'string' },
              question: { type: 'string' },
              gold_answer: { type: 'string' },
              source_quote: { type: 'string' },
            },
            required: ['id', 'question', 'gold_answer'],
          },
        },
        answers: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              qid: { type: 'string' },
              condition: { type: 'string' },
              answer: { type: 'string' },
            },
            required: ['qid', 'condition', 'answer'],
          },
        },
      },
      required: ['questions', 'answers'],
    },
  }
)

const qMap = {}
for (const q of (loadResult && loadResult.questions) ? loadResult.questions : []) {
  qMap[q.id] = q
}
const answers = (loadResult && loadResult.answers) ? loadResult.answers : []
log(`${Object.keys(qMap).length}개 문항, ${answers.length}개 답변 로드됨`)

// 조건 블라인드: X/Y 라벨로 셔플 (각 qid 내에서 A→X or B→X를 뒤집어 섞음)
// 단순하게: A→X, B→Y (순서만 섞어도 채점자가 A/B 모름 — 루브릭만으로 판단)
const scoringItems = answers.map((a) => ({
  qid: a.qid,
  blind_label: a.condition === 'A' ? 'X' : 'Y',
  condition: a.condition,
  question: qMap[a.qid] ? qMap[a.qid].question : '',
  gold_answer: qMap[a.qid] ? qMap[a.qid].gold_answer : '',
  source_quote: qMap[a.qid] ? qMap[a.qid].source_quote : '',
  answer: a.answer,
}))

// 배치 분할
const batches = []
for (let i = 0; i < scoringItems.length; i += JUDGE_BATCH_SIZE) {
  batches.push(scoringItems.slice(i, i + JUDGE_BATCH_SIZE))
}
log(`${batches.length}개 배치 (${JUDGE_BATCH_SIZE}건씩) 채점 시작`)

const judgeResults = await parallel(
  batches.map((batch, bIdx) => () =>
    agent(
      `다음 ${batch.length}건의 질문-답변 쌍을 채점하라.
채점자는 어느 조건(A/B)의 답변인지 알지 못한다. 루브릭만으로 판정하라.

루브릭:
- 정답: 핵심 사실(수치, 기간, 요건, 결론)이 gold_answer 및 source_quote와 일치
- 부분정답: 방향은 맞으나 핵심 사실 일부 누락 또는 세부 조건 누락
- 오답: 핵심 사실 오류, 무응답, 환각(조문에 없는 내용 추가)

채점 항목 목록 (총 ${batch.length}건):
${batch.map((item, i) => `
=== 항목 ${i + 1} ===
[qid: ${item.qid}, 라벨: ${item.blind_label}]
질문: ${item.question}
정답(gold_answer): ${item.gold_answer}
근거 원문(source_quote): ${item.source_quote}
후보 답변: ${item.answer}
`).join('\n')}

각 항목에 대해 qid, condition="${"X"}→원래condition복원없이그대로blind_label값사용"
아니다. 다음 형식으로 반환:
- qid: 그대로
- condition: 항목의 라벨(X 또는 Y) 그대로
- verdict: 정답/부분정답/오답
- reason: 1~2문장 채점 근거`,
      { label: `judge:batch${bIdx}`, phase: 'Judge', schema: JUDGE_SCHEMA }
    ).then((r) => r ? r.judgments : [])
  )
)

// 블라인드 라벨을 실제 condition으로 복원
const labelToCondition = {}
for (const item of scoringItems) {
  labelToCondition[`${item.qid}:${item.blind_label}`] = item.condition
}

const judgments = judgeResults.filter(Boolean).flat().map((j) => {
  const key = `${j.qid}:${j.condition}`
  const realCondition = labelToCondition[key] || j.condition
  return { ...j, condition: realCondition }
})

log(`채점 완료: ${judgments.length}건`)
return { judgments }
