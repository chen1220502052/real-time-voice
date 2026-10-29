from scripts.shadow_audio_review import candidate, roles, sentence_units, split_long


def transcript(sentences):
    words = []
    for start, text in sentences:
        for index, word in enumerate(text.split()):
            words.append({'start': round(start + index * .3, 2), 'end': round(start + index * .3 + .2, 2),
                          'word': ' ' + word, 'probability': .95})
    return {'duration':60, 'segments':[{'words':words}]}


def row():
    return {'sha256':'source','duration':60,'uses':[{'title':'Section A 1b'}],
            'fullLesson':False,'kinds':['passage']}


def test_opening_topic_question_is_not_mistaken_for_dialogue():
    data = transcript([(0,'Section A.'),(2,'How do people spend their holidays?'),
                       (6,'Section A.'),(8,'Activity 1B.'),(10,'Listen to the conversations and circle the activities.'),
                       (16,'Conversation one.'),(19,'Hi Emma.'),(21,'How was your vacation?')])
    draft = candidate(row(),data)
    assert draft['status'] == 'draft'
    assert [u['role'] for u in draft['units'] if u['text']=='How do people spend their holidays?'] == ['title']
    assert all(c['start'] >= 18 for c in draft['clips'])
    assert any('How was your vacation?' in c['text'] for c in draft['clips'])


def test_markers_without_punctuation_do_not_delete_following_content():
    data = transcript([(0,'Read and write Read and match I smell with my nose.')])
    units = roles(sentence_units(data))
    assert [u['text'] for u in units if u['role']=='content'] == ['I smell with my nose.']


def test_pause_inside_a_sentence_does_not_create_an_incomplete_prompt():
    data = transcript([(0,'Where'),(2,'are you from?')])
    draft = candidate(row(),data)
    assert len(draft['clips']) == 1
    assert draft['clips'][0]['text'] == 'Where are you from?'


def test_short_dialogue_and_real_lets_talk_sentence_are_preserved():
    data = transcript([(0,"Let's talk about our school."),(5,'Yes!')])
    units = roles(sentence_units(data))
    assert all(u['role']=='content' for u in units)
    assert any('Yes!' in c['text'] for c in candidate(row(),data)['clips'])


def test_long_continuous_sentence_is_held_instead_of_cut_at_arbitrary_seconds():
    data = transcript([(0,'This is a deliberately long sentence without any clause boundary.')])
    words = data['segments'][0]['words']
    for i,w in enumerate(words):w['start']=i*2;w['end']=i*2+1
    draft = candidate(row(),data)
    assert 'long_sentence_without_safe_clause_boundary' in draft['issues']
    assert draft['clips'] == []


def test_removed_instruction_is_a_barrier_to_combining_dialogue():
    data = transcript([(0,'Hello.'),(1,'Listen and repeat.'),(3,'Goodbye.')])
    draft = candidate(row(),data)
    assert len(draft['clips']) == 2
    assert all('Listen' not in c['text'] for c in draft['clips'])
    assert draft['clips'][0]['end'] <= draft['clips'][1]['start']


def test_song_and_full_recording_are_audited_but_stay_listening_resources():
    for patch in [{'fullLesson':True}, {'uses':[{'title':'Listen and sing'}]}, {'kinds':['words']}]:
        value = row() | patch
        draft = candidate(value,transcript([(0,'Listen and sing.'),(4,'Hello happy world.')]))
        assert draft['listenOnly'] and draft['clips']==[]
        assert draft['units']


def test_look_sentence_in_dialogue_is_not_a_task_instruction():
    data = transcript([(0,"Let's talk."),(4,'Look, Binbin and John are watering the vegetables.'),
                       (8,'Oh yes, we can help too.')])
    draft = candidate(row(),data)
    assert any('watering the vegetables' in c['text'] for c in draft['clips'])


def test_a_header_followed_by_a_number_does_not_delete_the_first_question():
    data = transcript([(0,"Let's learn A 1 listen look and say Let's have some fruit."),
                       (10,'Do you like oranges?')])
    draft = candidate(row(),data)
    assert any("Let's have some fruit." in c['text'] for c in draft['clips'])
    assert any('Do you like oranges?' in c['text'] for c in draft['clips'])


def test_exercise_numbers_are_not_section_titles_and_never_delete_dialogue():
    data=transcript([(0,'2.'),(1,'Listen and number the pictures.'),(5,'1.'),
                     (6,'Is it blue?'),(9,'No.'),(12,'Is it yellow?'),(15,'No.'),
                     (18,'2.'),(20,"Let's play."),(23,'What colour is my fish?'),
                     (34,"Let's read."),(38,'Listen and read.'),(42,'Hello there.')])
    result=candidate(row(),data)
    assert any('Is it blue?' in c['text'] for c in result['clips'])
    assert any('Is it yellow?' in c['text'] for c in result['clips'])
    assert all('number the pictures' not in c['text'] for c in result['clips'])


def test_source_bound_semantic_review_can_retain_a_real_dialogue_invitation():
    from scripts.shadow_audio_review import checksum
    data=transcript([(0,'Hello there.'),(20,"Let's play!"),(24,'Okay!')])
    raw=candidate(row(),data)
    assert 'instruction_inside_content_needs_context_review' in raw['issues']
    u=next(u for u in raw['units'] if u['text']=="Let's play!")
    correction={'transcriptDigest':checksum(data),'roleEdits':[{'text':u['text'],'start':u['start'],
                'role':'content','reason':'an invitation within the dialogue'}]}
    fixed=candidate(row(),data,correction)
    assert 'instruction_inside_content_needs_context_review' not in fixed['issues']
    assert any("Let's play!" in c['text'] for c in fixed['clips'])


def test_segment_only_recognition_is_allowed_for_listening_and_blocked_for_follow_targets():
    import pytest
    data={'duration':60,'segments':[{'start':0,'end':30,'text':'A full listening paragraph.',
                                   'avg_logprob':-.1,'words':None}]}
    assert candidate(row()|{'fullLesson':True},data)['listenOnly']
    with pytest.raises(ValueError,match='Word alignment is required'):candidate(row(),data)


def test_english_pronoun_i_is_never_parsed_as_an_exercise_number():
    data=transcript([(0,'Start to read.'),(4,'I say hi.'),(8,'I listen.'),(12,'I share.')])
    result=candidate(row(),data)
    assert any('I listen.' in c['text'] for c in result['clips'])
    assert all(u['role']=='content' for u in result['units'] if u['text'].startswith('I '))


def test_complete_instruction_object_is_not_split_into_a_fake_body_prompt():
    data=transcript([(0,'Listen and circle what you hear.'),(7,'Hello, John!')])
    result=candidate(row(),data)
    assert all('what you hear' not in c['text'] for c in result['clips'])
    assert any('Hello, John!' in c['text'] for c in result['clips'])


def test_invitation_as_the_first_dialogue_line_requires_semantic_review():
    data=transcript([(0,"Let's learn A."),(3,'Listen, look and say.'),(7,"Let's play!"),(9,'Okay!')])
    result=candidate(row(),data)
    assert 'instruction_inside_content_needs_context_review' in result['issues']


def test_fragmented_true_false_answer_format_is_not_a_follow_reading_prompt():
    data=transcript([(0,'Listen and write T.'),(2,'True or F.'),(3,'False.'),(8,'Hello, John!')])
    result=candidate(row(),data)
    assert len(result['clips'])==1
    assert result['clips'][0]['text']=='Hello, John!'


def test_a_song_inside_a_mixed_recording_does_not_replace_the_spoken_dialogue():
    data=transcript([(0,'Listen and say.'),(4,'Hello, Sam!'),(8,'Hi, Tom!'),
                     (15,'Listen and sing.'),(20,'The hello song.'),(25,'Hello, hello, hello!')])
    result=candidate(row(),data)
    assert any('Hello, Sam!' in c['text'] for c in result['clips'])
    assert all('hello song' not in c['text'] and 'hello, hello' not in c['text'] for c in result['clips'])
    assert any(u['role']=='listening' for u in result['units'])


def test_reviewed_title_prefix_keeps_the_first_sentence_when_asr_omits_punctuation():
    from scripts.shadow_audio_review import checksum
    data=transcript([(0,'A cooking accident James lived in the city with his friend Alan.')])
    correction={'transcriptDigest':checksum(data),'prefixEdits':[{'prefix':'A cooking accident','start':0,
                'role':'title','reason':'reading title before the first narrative sentence'}]}
    result=candidate(row(),data,correction)
    assert any(c['text'].startswith('James lived') for c in result['clips'])
    assert all('A cooking accident' not in c['text'] for c in result['clips'])


def test_reviewed_sentence_breaks_preserve_original_transcript_and_source_digest():
    import copy
    from scripts.shadow_audio_review import checksum
    data=transcript([(0,'Spring is green I like summer.')]);before=copy.deepcopy(data)
    word=data['segments'][0]['words'][2]
    correction={'transcriptDigest':checksum(data),'wordBreaks':[{'word':word['word'],'start':word['start']}]}
    result=candidate(row(),data,correction)
    assert data==before
    assert result['transcriptDigest']==checksum(before)
    assert [u['text'] for u in result['units']]==['Spring is green.','I like summer.']


def test_repeated_numbers_and_phone_digits_remain_practice_targets():
    data=transcript([(0,'Listen and number the words above.'),(5,'Sixteen.'),
                     (7,'Sixteen.'),(10,'Seventeen.'),(12,'Seventeen.'),
                     (16,'Eight.'),(18,'Six.'),(20,'Zero.'),(22,'Four.')])
    units=roles(sentence_units(data))
    assert all(u['role']=='content' for u in units if u['start']>=5)


def test_reviewed_glossary_is_listening_reference_with_explicit_reason():
    from scripts.shadow_audio_review import checksum
    t=transcript([(0,'Unit 1.'),(3,'Coat.'),(5,'Coat.'),(8,'Sleep.'),(10,'Sleep.')])
    draft=candidate(row(),t,{'transcriptDigest':checksum(t),
                            'listenOnlyReason':'Reviewed term vocabulary list; individual words have their own practice targets.'})
    assert draft['listenOnly'] and draft['clips']==[]
    assert not candidate(row(),t,{'transcriptDigest':checksum(t),'listenOnlyReason':''})['listenOnly']


def test_generic_recording_with_only_song_content_stays_full_listening():
    t=transcript([(0,'Listen and sing.'),(4,'We sing a happy song.'),(10,'We play all day.')])
    assert candidate(row(),t)['listenOnly']
    mixed=transcript([(0,'Hello Tom.'),(2,'Hi Jane.'),(5,'Listen and sing.'),(9,'We sing a happy song.')])
    assert not candidate(row(),mixed)['listenOnly']
    assert any('Hello Tom' in c['text'] for c in candidate(row(),mixed)['clips'])


def test_weather_report_task_removes_opening_unit_topic_and_preserves_news_body():
    t=transcript([(0,'Unit 6.'),(2,'Rain or shine.'),(4,'Big question.'),
                  (6,'How does the weather affect us?'),(10,'Section A.'),
                  (12,'Activity 1B.'),(15,'Listen to the weather report.'),
                  (19,'Match the places with the weather conditions.'),
                  (23,'Good morning Australia.'),(26,'It is sunny in Sydney today.')])
    d=candidate(row(),t)
    assert all(c['start']>22 for c in d['clips'])
    assert any('sunny in Sydney' in c['text'] for c in d['clips'])


def test_comma_and_then_task_formats_are_excluded_without_deleting_body():
    t=transcript([(0,'Listen, choose and complete.'),(4,'One.'),(7,'Classroom.'),
                  (10,'Then listen and read it again at a normal speed.'),(16,'Hi Sam.')])
    d=candidate(row(),t)
    assert not any('complete' in c['text'] or 'normal speed' in c['text'] for c in d['clips'])
    assert any('Classroom' in c['text'] for c in d['clips'])
    assert any('Hi Sam' in c['text'] for c in d['clips'])


def test_first_conversation_setup_question_is_not_a_childs_dialogue():
    t=transcript([(0,'Section A.'),(2,'Listen to three conversations.'),
                  (6,'Why do the guests have these feelings?'),(11,'Conversation one.'),
                  (14,'Hi Kaito.'),(17,'You are new here, right?')])
    d=candidate(row(),t)
    assert not any('guests' in c['text'] for c in d['clips'])
    assert any('You are new here' in c['text'] for c in d['clips'])


def test_sound_practice_before_a_conversation_is_not_narrator_setup():
    t=transcript([(0,'Activity 1.'),(2,'Listen and repeat.'),(6,'Boat.'),
                  (8,'Coat.'),(12,'Conversation one.'),(15,'Hello Tom.')])
    d=candidate(row(),t)
    assert any('Boat' in c['text'] for c in d['clips'])
