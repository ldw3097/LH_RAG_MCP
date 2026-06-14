export const meta = {
  name: 'eval-wf3-judge-full',
  description: '평가 WF-3 (전체): 1,000건 블라인드 채점 (에이전트별 직접 읽기)',
  phases: [
    { title: 'Judge', detail: '50배치 병렬 채점, 에이전트가 직접 파일 읽기' },
    { title: 'Merge', detail: 'temp 파일 합산 → full_judgments.jsonl 저장' },
  ],
}

const ANSWERS_PATH   = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/full_answers.jsonl'
const QUESTIONS_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/full_questions.jsonl'
const OUTPUT_PATH    = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/full_judgments.jsonl'
const TMP_DIR        = '/tmp/wf3_judgments'
const BATCH_SIZE     = 20
const TOTAL          = 1000

// full_answers.jsonl 구조:
//   0~499  : condA (lh×100, kcsc×100, pps×100, law×100, prec×100 순)
//   500~999: condB (동일 순서)
// full_questions.jsonl 구조:
//   lh:0~99  kcsc:100~199  pps:200~299  law:300~399  prec:400~499

const NUM_BATCHES = TOTAL / BATCH_SIZE  // 50

// ─── Judge: 50배치 병렬 ────────────────────────────────────────────────
phase('Judge')

await parallel(
  Array.from({ length: NUM_BATCHES }, (_, bi) => () =>
    agent(
      `Judge 배치 ${bi} (answers.jsonl 줄 ${bi * BATCH_SIZE}~${bi * BATCH_SIZE + BATCH_SIZE - 1})

【Step 1】 데이터 읽기
Bash로 아래 Python 실행:
python3 << 'PYEOF'
import json, os

ANSWERS_PATH   = '${ANSWERS_PATH}'
QUESTIONS_PATH = '${QUESTIONS_PATH}'
CAT_OFFSETS    = {'lh':0,'kcsc':100,'pps':200,'law':300,'prec':400}
LINE_START = ${bi * BATCH_SIZE}
BATCH_SIZE = ${BATCH_SIZE}

with open(ANSWERS_PATH) as f:
    all_ans = f.readlines()
with open(QUESTIONS_PATH) as f:
    all_q = f.readlines()

items = []
for i in range(BATCH_SIZE):
    a = json.loads(all_ans[LINE_START + i])
    qid = a['qid']
    cat, num = qid.rsplit('-', 1)
    q = json.loads(all_q[CAT_OFFSETS[cat] + int(num) - 1])
    items.append({
        'qid':          a['qid'],
        'condition':    a['condition'],
        'blind_label':  'X' if a['condition'] == 'A' else 'Y',
        'question':     q['question'],
        'gold_answer':  q['gold_answer'],
        'source_quote': q.get('source_quote', ''),
        'answer':       a['answer'],
    })
    print(json.dumps(items[-1], ensure_ascii=False))
PYEOF

【Step 2】 채점
위에서 읽은 ${BATCH_SIZE}개 항목을 채점하라.
채점자는 조건(A/B)을 모른다 — blind_label(X/Y)만 보고 루브릭으로 판정.

루브릭:
- 정답: 핵심 사실(수치, 기간, 요건, 결론)이 gold_answer 및 source_quote와 일치
- 부분정답: 방향은 맞으나 핵심 사실 일부 누락 또는 세부 조건 누락
- 오답: 핵심 사실 오류, 무응답, 환각(조문에 없는 내용 추가)

【Step 3】 temp 파일 저장
Bash: mkdir -p ${TMP_DIR}
그 다음 Write 도구로 "${TMP_DIR}/batch_${bi}.jsonl" 파일 생성.
각 줄 형식 (condition은 blind_label 아닌 실제 A/B):
{"qid":"...","condition":"A","verdict":"정답/부분정답/오답","reason":"1~2문장"}`,
      { label: `judge:batch${bi}`, phase: 'Judge' }
    )
  )
)

log('채점 완료, 합산 중...')

// ─── Merge ─────────────────────────────────────────────────────────────
phase('Merge')

await agent(
  `Bash 도구로 아래 Python을 실행하여 temp 파일들을 합산하고 "${OUTPUT_PATH}"에 저장하라:

python3 << 'PYEOF'
import json, os

tmp_dir  = '${TMP_DIR}'
out_path = '${OUTPUT_PATH}'
n_batches = ${NUM_BATCHES}

all_judgments = []
missing = []

for i in range(n_batches):
    fname = f'{tmp_dir}/batch_{i}.jsonl'
    if os.path.exists(fname):
        with open(fname) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        all_judgments.extend(rows)
    else:
        missing.append(i)

with open(out_path, 'w') as f:
    for j in all_judgments:
        f.write(json.dumps(j, ensure_ascii=False) + '\\n')

print(f'저장 완료: {out_path} ({len(all_judgments)}건)')
if missing:
    print(f'누락 배치: {missing}')
PYEOF`,
  { label: 'merge-save', phase: 'Merge' }
)

log(`완료 → ${OUTPUT_PATH}`)
return { output: OUTPUT_PATH }
