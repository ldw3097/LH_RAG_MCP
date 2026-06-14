export const meta = {
  name: 'eval-wf2-answers-full',
  description: '평가 WF-2 (전체): 500문항 × 2조건 답변 수집 + 소요시간 측정',
  phases: [
    { title: 'CondA', detail: '웹검색만 허용, MCP 없음' },
    { title: 'CondB', detail: '웹+MCP 툴 사용' },
    { title: 'Merge', detail: 'temp 파일 합산 → full_answers.jsonl 저장' },
  ],
}

const QUESTIONS_PATH = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/full_questions.jsonl'
const OUTPUT_PATH    = '/Users/ldw/code/LH_RAG_MCP/code/eval/llm_eval/full_answers.jsonl'
const TMP_DIR        = '/tmp/wf2_answers'
const BATCH_SIZE     = 10

// 카테고리별 QUESTIONS_PATH 줄 오프셋 (사전 확인 완료)
// lh:0~99  kcsc:100~199  pps:200~299  law:300~399  prec:400~499
const CAT_OFFSETS = { lh: 0, kcsc: 100, pps: 200, law: 300, prec: 400 }
const CATEGORIES  = ['lh', 'kcsc', 'pps', 'law', 'prec']
const PER_CAT     = 100

const MCP_TOOL_HINTS = {
  lh:   'ToolSearch query "search_lh_regulations"',
  kcsc: 'ToolSearch query "search_construction_standards"',
  pps:  'ToolSearch query "search_procurement_interpretations"',
  law:  'ToolSearch query "search_law"',
  prec: 'ToolSearch query "search_precedents"',
}

const batches = []
for (const cat of CATEGORIES) {
  for (let bi = 0; bi < PER_CAT / BATCH_SIZE; bi++) {
    batches.push({ cat, bi, lineStart: CAT_OFFSETS[cat] + bi * BATCH_SIZE })
  }
}
log(`${batches.length}개 배치 × 2조건 = ${batches.length * 2}개 에이전트`)

// ─── 조건 A: 웹검색만, MCP 금지 ──────────────────────────────────────
phase('CondA')

await parallel(
  batches.map((b) => () =>
    agent(
      `카테고리: ${b.cat}, 배치: ${b.bi} (조건 A — 웹검색 전용)

【Step 1】 시작 시각 기록
Bash: date +%s  → start_ts 저장

【Step 2】 문항 읽기
Bash로 아래 Python 실행:
python3 -c "
import json
with open('${QUESTIONS_PATH}') as f: lines = f.readlines()
for i in range(${BATCH_SIZE}):
    q = json.loads(lines[${b.lineStart}+i])
    print(q['id'] + ' ||| ' + q['question'])
"

【Step 3】 답변 작성
각 질문에 2~4문장으로 답하라.
규칙:
- WebSearch·WebFetch 도구만 허용
- data/ eval/ 디렉터리 파일 Read 금지
- ToolSearch·MCP 툴 사용 금지
- 웹에서 찾지 못하면 "웹 검색으로 확인 불가" 명시

【Step 4】 종료 시각 기록
Bash: date +%s  → end_ts 저장, duration_s = end_ts - start_ts

【Step 5】 temp 파일 저장
Bash: mkdir -p ${TMP_DIR}
그 다음 Write 도구로 "${TMP_DIR}/condA_${b.cat}_${b.bi}.jsonl" 파일 생성.
파일 내용: 수집한 10개 답변을 JSONL 형식으로 (한 줄 = JSON 한 개).
각 줄 형식: {"qid":"...","condition":"A","answer":"...","tools_used":[...],"duration_s":N}
qid는 Step 2에서 읽은 id 값 그대로.`,
      { label: `condA:${b.cat}:${b.bi}`, phase: 'CondA' }
    )
  )
)
log('조건A 완료')

// ─── 조건 B: 웹+MCP ────────────────────────────────────────────────────
phase('CondB')

await parallel(
  batches.map((b) => () =>
    agent(
      `카테고리: ${b.cat}, 배치: ${b.bi} (조건 B — MCP 우선)

【Step 1】 시작 시각 기록
Bash: date +%s  → start_ts 저장

【Step 2】 문항 읽기
Bash로 아래 Python 실행:
python3 -c "
import json
with open('${QUESTIONS_PATH}') as f: lines = f.readlines()
for i in range(${BATCH_SIZE}):
    q = json.loads(lines[${b.lineStart}+i])
    print(q['id'] + ' ||| ' + q['question'])
"

【Step 3】 MCP 툴 로드 후 답변
먼저 ${MCP_TOOL_HINTS[b.cat]} 로 MCP 툴을 로드하여 우선 사용하라.
MCP 결과로 충분하면 웹 검색 불필요. 필요시 WebSearch/WebFetch로 보완.
각 질문에 2~4문장으로 답하라.

【Step 4】 종료 시각 기록
Bash: date +%s  → end_ts 저장, duration_s = end_ts - start_ts

【Step 5】 temp 파일 저장
Bash: mkdir -p ${TMP_DIR}
그 다음 Write 도구로 "${TMP_DIR}/condB_${b.cat}_${b.bi}.jsonl" 파일 생성.
파일 내용: 수집한 10개 답변을 JSONL 형식으로 (한 줄 = JSON 한 개).
각 줄 형식: {"qid":"...","condition":"B","answer":"...","tools_used":[...],"duration_s":N}
qid는 Step 2에서 읽은 id 값 그대로.`,
      { label: `condB:${b.cat}:${b.bi}`, phase: 'CondB' }
    )
  )
)
log('조건B 완료')

// ─── Merge ─────────────────────────────────────────────────────────────
phase('Merge')

await agent(
  `Bash 도구로 아래 Python을 실행하여 temp 파일들을 합산하고 "${OUTPUT_PATH}"에 저장하라:

python3 << 'PYEOF'
import json, os

tmp_dir = '${TMP_DIR}'
out_path = '${OUTPUT_PATH}'
cats = ['lh','kcsc','pps','law','prec']
batches_per_cat = 10

all_answers = []
missing = []

for cond in ['condA', 'condB']:
    for cat in cats:
        for bi in range(batches_per_cat):
            fname = f'{tmp_dir}/{cond}_{cat}_{bi}.jsonl'
            if os.path.exists(fname):
                with open(fname) as f:
                    rows = [json.loads(l) for l in f if l.strip()]
                all_answers.extend(rows)
            else:
                missing.append(f'{cond}_{cat}_{bi}')

with open(out_path, 'w') as f:
    for row in all_answers:
        f.write(json.dumps(row, ensure_ascii=False) + '\\n')

print(f'저장 완료: {out_path} ({len(all_answers)}건)')
if missing:
    print(f'누락 {len(missing)}개: {missing}')
PYEOF`,
  { label: 'merge-save', phase: 'Merge' }
)

log(`완료 → ${OUTPUT_PATH}`)
return { output: OUTPUT_PATH }
