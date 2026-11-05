/* Small, source-backed practice targets. Long lessons are listening extras. */
(function (root) {
  function seconds(value) { return `${Math.max(1, Math.round(value))} 秒`; }
  function title(recording, index) {
    const text = recording.practiceTitle || recording.title || '';
    if (/^(整课|教材分段)/.test(text)) return `看图听读 · 第 ${index + 1} 部分`;
    return text.replace(/Get Ready/gi, '热身练习').replace(/Let[’']s Learn/gi, '学一学')
      .replace(/Let[’']s Read/gi, '故事阅读').replace(/Phonics/gi, '发音练习')
      .replace(/Listen, look and say/gi, '看图说一说').replace(/Listen, point and repeat/gi, '听一听，读一读')
      .replace(/Listen and repeat/gi, '听一听，读一读').replace(/Listen and chant/gi, '读歌谣')
      .replace(/Listen and sing/gi, '唱一唱').replace(/Let[’']s talk/gi, '对话练习');
  }
  function groups(chapter, kind) {
    if (!chapter) return [];
    if (kind === 'words') return [{id: 'words', label: '本课单词', targets: (chapter.vocabulary || []).map((word, index) => ({
      key: `word:${index}:${word.word}`, label: word.word, meaning: word.meaning,
      source: word.sampleAudio?.url ? {url: word.sampleAudio.url} : null,
      sourceLabel: word.sampleAudio?.kind === 'synthetic' ? '合成示范发音' : '单词示范录音',
      duration: word.sampleAudio?.duration, type: 'word',
    }))}].filter(group => group.targets.length);
    return (chapter.recordings || []).filter(r => r.kind === 'passage' && r.url && !r.fullLesson && !/整课/.test(r.title))
      .map((recording, index) => {
        const pages = (chapter.textbookPages || []).filter(p => p.recordingId === recording.id);
        const lines = pages.flatMap(page => (page.scenes || []).flatMap(scene => scene.lines.map(line => ({...line, page}))));
        const label = title(recording, index);
        const aligned = recording.alignedSentences || [];
        const sourceLabel = recording.sourceLabel || '课本原音';
        const targets = lines.length ? lines.map((line, i) => ({
          key: `line:${recording.id}:${i}`, label: line.text, page: line.page, type: 'sentence',
          source: {url: recording.url, start: line.start, end: line.end}, sourceLabel, duration: line.end-line.start,
        })) : aligned.length ? aligned.map((line, i) => ({
          key: `aligned:${recording.id}:${i}`, label: line.text, meaning: line.chinese || '', type: 'sentence',
          page:recording.referencePage || pages[0],
          source: {url: recording.url, start: line.start, end: line.end}, sourceLabel, duration: line.end-line.start,
        })) : (recording.practiceClips || []).map((clip, i) => ({
          key: `clip:${recording.id}:${i}`, label: `第 ${i+1} 小段`, page: pages[0], type: 'excerpt',
          source: {url: recording.url, start: clip.start, end: clip.end}, sourceLabel, duration: clip.end-clip.start,
        }));
        return {id: recording.id, label, targets: targets.filter(t => t.duration > 0 && t.duration <= 18.05)};
      }).filter(group => group.targets.length && !/^听力|^歌曲|唱一唱/.test(group.label))
      .sort((a,b) => priority(a.label)-priority(b.label));
  }
  function priority(text) { return /对话|talk/i.test(text) ? 0 : /故事|read/i.test(text) ? 1 : /歌|sing|chant/i.test(text) ? 3 : 2; }
  const api = {groups, title, seconds};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KittyShadowContent = api;
})(typeof window !== 'undefined' ? window : globalThis);
