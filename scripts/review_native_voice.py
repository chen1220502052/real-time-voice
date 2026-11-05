"""Materialize the assistant's case-by-case content review, separate from rules."""
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'evals/voice_optimization'
MODELS = ['qwen2.5:3b', 'qwen3.5:4b', 'qwen3.5:4b-native-mlx-affine4']


def main():
    data = json.loads((DEST / 'native-results.json').read_text())
    overrides = {}

    def mark(model, phase, identity, repeats, status, reason):
        for repeat in repeats:
            overrides[(MODELS[model], phase, identity, repeat)] = (status, reason)

    # Review of all 288 custom text responses. Style and regex-only mismatches
    # are retained in the original rule score rather than relabeled as facts.
    for identity in ['q35-21', 'q35-23', 'q35-24', 'opt-hold-03',
                     'opt-hold-05', 'opt-hold-08', 'opt-hold-12', 'opt-accept-02']:
        reason = {
            'q35-21': 'First response omits the banana example; second has ungrammatical count-noun examples.',
            'q35-23': 'Repeats the question rather than stating that the passage lacks a companion.',
            'q35-24': 'Application rejected the generated reply; no answer delivered.',
            'opt-hold-03': 'Infers rabbit diet from general knowledge rather than admitting the story does not say.',
            'opt-hold-05': 'Describes asking a cousin instead of producing the requested direct invitation.',
            'opt-hold-08': 'Claims pronunciation is correct despite receiving only a transcript.',
            'opt-hold-12': 'Application rejected the generated reply; no answer delivered.',
            'opt-accept-02': 'Speculates about bag contents absent from the passage.',
        }[identity]
        mark(0, 'text', identity, [0, 1], 'fail', reason)
    mark(0, 'text', 'q35-12', [1], 'fail', 'Teaches rabbit as wabbit, an incorrect pronunciation.')
    mark(0, 'text', 'q35-08', [1], 'fail', 'Answers either bike or bus after history explicitly updated to bus.')
    mark(0, 'text', 'q35-20', [0, 1], 'needs_review', 'Adds name Ruby after the pet, creating an unsupported pet-name reading.')
    mark(0, 'text', 'opt-accept-06', [0], 'needs_review', 'Awkward Chinese clause makes the name Nell referent unclear.')
    for model in [1, 2]:
        for identity in ['q35-24', 'opt-hold-12']:
            mark(model, 'text', identity, [0, 1], 'fail', 'Application rejected the generated reply; no answer delivered.')
        mark(model, 'text', 'opt-hold-05', [0, 1], 'fail', 'Asks about inviting or describes asking rather than directly inviting the cousin.')
        mark(model, 'text', 'q35-01', [0, 1], 'needs_review', 'Main protection fact is correct; extra claims about shell warmth or being very light need verification.')
    mark(1, 'text', 'q35-02', [1], 'fail', 'Says ducks lay eggs on land or water; water is not a normal duck nesting site.')
    mark(1, 'text', 'opt-hold-09', [0], 'fail', 'Adds you ask me, changing the requested direct translation into reported speech.')
    mark(1, 'text', 'opt-accept-04', [1], 'fail', 'Delivers two sentences when the request explicitly asks for one.')
    mark(2, 'text', 'q35-02', [0], 'fail', 'States that a cat cannot swim, a false absolute claim.')
    mark(2, 'text', 'opt-accept-07', [1], 'fail', 'Application rejected this Chinese subject answer, unlike both reference repetitions.')

    # Audio content is evaluated against the recognized input, so the common
    # upstream Lena to Lana ASR substitution is not a model-answer failure.
    for identity in ['opt-hold-03', 'opt-hold-05', 'opt-hold-08']:
        reason = {'opt-hold-03': 'Invents a diet absent from the story.',
                  'opt-hold-05': 'Instructions about asking instead of the requested invitation.',
                  'opt-hold-08': 'Claims correct pronunciation with transcript-only input.'}[identity]
        mark(0, 'audio', identity, [0, 1], 'fail', reason)
    mark(0, 'audio', 'q35-02', [1], 'fail', 'Says cats cannot swim and ducks walk on water.')
    mark(0, 'audio', 'q35-01', [0], 'needs_review', 'Unsupported extension that shells help movement.')
    mark(0, 'audio', 'q35-04', [0], 'needs_review', 'Bread is omitted and the referent of spreading butter on one slice is unclear.')
    for model in [1, 2]:
        mark(model, 'audio', 'opt-hold-05', [0, 1], 'fail', 'Does not give the requested direct invitation.')
        mark(model, 'audio', 'q35-01', [0, 1], 'needs_review', 'Correct protection fact with an unverified extension about shell warmth or weight.')
    mark(1, 'audio', 'q35-11', [0], 'needs_review', 'Example describes bright red cherry-tree flowers; botanical claim needs review.')
    mark(2, 'audio', 'opt-hold-06', [0], 'fail', 'Incorrectly describes a fork as designed to slice food into smaller pieces.')
    mark(2, 'audio', 'opt-hold-07', [0], 'needs_review', 'Example says a book is too light to stay on a table without a supporting situation.')

    reviews = []
    for row in data['rows']:
        if row['phase'] == 'text' and row['set'] == 'regression':
            continue
        status, reason = overrides.get((row['model'], row['phase'], row['id'], row['repeat']),
                                      ('pass', 'Requested core content is present and no clear content error identified; style remains in rule scores.'))
        if row.get('error'):
            assert status == 'fail'
        reviews.append({k: row[k] for k in ['model', 'phase', 'id', 'set', 'repeat', 'text']} |
                       {'status': status, 'reason': reason, 'rulePassed': row['score']['passed']})
    summary = {}
    for model in MODELS:
        summary[model] = {}
        for group in ['development', 'prior_holdout', 'acceptance', 'unseen_acceptance', 'custom_text', 'audio_content']:
            selected = []
            for row in reviews:
                if row['model'] != model:
                    continue
                if group == 'audio_content':
                    include = row['phase'] == 'audio'
                elif row['phase'] != 'text':
                    include = False
                elif group == 'custom_text':
                    include = True
                elif group == 'unseen_acceptance':
                    include = row['set'] == 'acceptance' and row['id'] != 'opt-accept-06'
                else:
                    include = row['set'] == group
                if include:
                    selected.append(row)
            summary[model][group] = dict(collections.Counter(r['status'] for r in selected))
    result = {'method': 'Assistant content review of every custom text reply and every delivered audio reply text. Not a teacher blind evaluation or MOS. Unverified extensions and ambiguous language are needs_review; application rejection is fail; regex false positives are not semantic failures.',
              'acceptanceDisclosure': 'opt-accept-06 was viewed during adapter debugging; only the other seven cases are unseen acceptance. No prompt or model changes followed final results.',
              'audioScope': 'These labels concern reply content. Independent WAV/transcription verification is recorded separately and does not establish human listening quality.',
              'summary': summary, 'rows': reviews}
    (DEST / 'native-content-review.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
