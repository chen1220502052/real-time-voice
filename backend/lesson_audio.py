"""Load local source-bound sentence audio without turning ASR drafts into textbook text."""
import hashlib
import json
import math


def add_local_lessons(library):
    from .chapter_practice import _id
    source=library.root/'本地语音处理/lesson-audio.json'
    if not source.is_file():return
    pack=json.loads(source.read_text())
    if pack.get('version')!=1:raise ValueError('Unsupported local lesson audio pack')

    def asset(entry, extensions):
        path=(library.root/entry['path']).resolve()
        if (not path.is_relative_to(library.root) or path.suffix.lower() not in extensions
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']):
            return None
        return path

    for chapter in pack['chapterAdditions']:
        # Additional source editions never replace a verified curriculum chapter.
        if chapter['id']!=_id(chapter['publisher'],chapter['grade'],chapter['edition'],chapter['unit']):
            raise ValueError('Incorrect local lesson chapter identity')
        library.chapters.setdefault(chapter['id'],{**chapter,'recordings':[]})
    for entry in pack['recordings']:
        chapter=library.chapters.get(entry['chapterId'])
        if chapter is None:continue
        if entry.get('chapterKey') and any(chapter[k]!=v for k,v in entry['chapterKey'].items()):
            raise ValueError('Local audio edition does not match chapter')
        path=asset(entry,{'.mp3','.wav'})
        if path is None:continue
        if entry.get('referencePath'):
            reference=(library.root/entry['referencePath']).resolve()
            if (not reference.is_relative_to(library.root) or not reference.is_file()
                    or hashlib.sha256(reference.read_bytes()).hexdigest()!=entry['referenceSha256']):
                continue
        duration=entry['duration']
        if not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0:
            raise ValueError('Invalid aligned audio duration')
        sentences=[]
        previous=0
        reference_page=None
        for line in entry['sentences']:
            start,end=line['start'],line['end']
            if (not all(isinstance(v,(int,float)) and math.isfinite(v) for v in [start,end])
                    or not previous<=start<end<=duration+.01 or end-start>18.05
                    or not isinstance(line['text'],str) or not line['text'].strip()):
                raise ValueError('Invalid aligned sentence')
            if line.get('evidencePath'):
                image=(library.root/line['evidencePath']).resolve()
                if (not image.is_relative_to(library.root) or not image.is_file()
                        or hashlib.sha256(image.read_bytes()).hexdigest()!=line['evidenceSha256']):
                    continue
                page_id=_id(chapter['id'],'aligned-reference-image',line['evidenceSha256'])
                library.textbook_pages[page_id]=image
                reference_page={'id':page_id,'url':'/api/practice/textbook-pages/'+page_id,'label':'对照教材原图'}
            sentences.append({k:line[k] for k in ['text','start','end','chinese','textSource','alignment'] if k in line})
            previous=end
        recording=next((r for r in chapter['recordings'] if library.audio.get(r['id'])==path),None)
        if recording is None:
            audio_id=_id(chapter['id'],'local-lesson-audio',entry['sha256'])
            library.audio[audio_id]=path
            recording={'id':audio_id,'kind':'passage','title':entry['title'],'category':'逐句听读',
                       'url':'/api/practice/audio/'+audio_id,'availability':'local','fullLesson':False,
                       'sourcePage':entry.get('sourcePage')}
            chapter['recordings'].append(recording)
        recording.update(duration=duration,alignedSentences=sentences,sourceLabel=entry['sourceLabel'],
                         audioKind=entry['audioKind'])
        if reference_page:recording['referencePage']=reference_page
        if entry.get('subtitle'):
            subtitle=asset(entry['subtitle'],{'.vtt'})
            if subtitle:
                library.subtitles[recording['id']]=subtitle
                recording['subtitleUrl']='/api/practice/subtitles/'+recording['id']
    for entry in pack.get('recoveredImages',[]):
        chapter=library.chapters.get(entry['chapterId']);path=asset(entry,{'.jpg','.png','.jpeg'})
        if not chapter or path is None:continue
        if any(hashlib.sha256(library.textbook_pages[p['id']].read_bytes()).hexdigest()==entry['sha256']
               for p in chapter.get('textbookPages',[]) if p['id'] in library.textbook_pages):
            continue
        page_id=_id(chapter['id'],'recovered-image',entry['sha256'])
        library.textbook_pages[page_id]=path
        chapter.setdefault('textbookPages',[]).append({'id':page_id,'url':'/api/practice/textbook-pages/'+page_id,
                                                       'label':entry['label'],'recordingId':None})
