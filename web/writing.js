"use strict";
(() => {
  const $ = id => document.getElementById(id), C = window.KittyCourses;
  let catalog = [], scopeVersion = 0, ready = false, busy = false, storage, activeChapter = '', draftTimer;
  const embedded = new URL(location.href).searchParams.get('embedded') === '1';
  if (embedded) {
    document.body.classList.add('writing-embedded');
    document.documentElement.classList.add('writing-embedded');
    // Fit the same-origin practice frame as drafts, examples and feedback grow.
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        const frame = window.frameElement;
        if (frame) frame.style.height = `${Math.ceil(document.querySelector('main').getBoundingClientRect().height) + 4}px`;
      }).observe(document.querySelector('main'));
    }
  }
  try { storage = window.KittyAccount ? window.KittyAccount.storage : localStorage; } catch { storage = null; }
  let preferred;
  const requested = new URL(location.href).searchParams.get('chapter');
  const draftKey = id => `kitty-writing-draft:${window.KittyAccount?.user?.id || 'local'}:${id}`;
  function saveDraft() {
    if (!activeChapter) return;
    try {
      localStorage.setItem(draftKey(activeChapter), JSON.stringify({title:$('studentTitle').value, essay:$('studentEssay').value}));
      $('draftStatus').textContent = '草稿已保存在本机';
    } catch { $('draftStatus').textContent = '本机无法保存草稿，请先复制内容，避免关闭后丢失。'; }
  }
  function restoreDraft(id) {
    clearTimeout(draftTimer); saveDraft(); activeChapter = id;
    $('studentTitle').value = ''; $('studentEssay').value = ''; $('reviewResult').hidden = true;
    try {
      const draft = JSON.parse(localStorage.getItem(draftKey(id)) || '{}');
      $('studentTitle').value = typeof draft.title === 'string' ? draft.title : '';
      $('studentEssay').value = typeof draft.essay === 'string' ? draft.essay : '';
    } catch { $('draftStatus').textContent = '草稿读取失败，请保留本次写作内容。'; }
  }
  function setBusy(value, action) {
    busy = value; $('writingFields').disabled = value; $('studentFields').disabled = value;
    for (const id of ['generate','reviewEssay']) $(id).disabled = value || !ready;
    $('generate').textContent = value && action === 'generate' ? '正在生成范文…' : '生成范文';
    $('reviewEssay').textContent = value && action === 'review' ? '正在批改…' : '批改作文';
  }
  for (const id of ['studentTitle','studentEssay']) $(id).addEventListener('input', () => {
    $('reviewResult').hidden = true; clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 400);
  });
  window.addEventListener('pagehide', saveDraft);
  function options(id, rows, value = x => x, label = value, wanted) {
    $(id).replaceChildren(...rows.map(row => new Option(label(row), value(row))));
    if ([...$(id).options].some(o => o.value === wanted)) $(id).value = wanted;
  }
  const rows = () => catalog.filter(c => c.publisher === $('publisher').value && c.grade === $('grade').value);
  function edition(wanted) {
    options('edition', [...new Set(rows().map(c => c.edition))], undefined, C.editionLabel, wanted);
    chapter();
  }
  function grade(wanted) {
    options('grade', [...new Set(catalog.filter(c => c.publisher === $('publisher').value).map(c => c.grade))]
      .sort((a,b) => parseInt(a)-parseInt(b)), undefined, undefined, wanted);
    edition(preferred.edition);
  }
  function chapter(wanted) {
    options('chapter', rows().filter(c => c.edition === $('edition').value), c => c.id, c => c.unit, wanted);
    loadScope();
  }
  async function loadScope() {
    const version = ++scopeVersion, id = $('chapter').value;
    ready = false; setBusy(false); $('result').hidden = true;
    restoreDraft(id);
    $('unitTitle').textContent = catalog.find(c => c.id === id)?.unit || '请选择教材单元';
    $('writingStatus').textContent = id ? '正在准备…' : '暂无教材单元。';
    if (!id) return;
    try {
      const response = await fetch('/api/writing/scopes/' + encodeURIComponent(id));
      const scope = await response.json();
      if (!response.ok) throw new Error('这个单元暂时无法使用，请稍后再试。');
      if (version !== scopeVersion) return;
      ready = scope.textbookReady; setBusy(false);
      $('vocabularyModeLabel').hidden = !scope.naturalWritingAvailable;
      if (!scope.naturalWritingAvailable) $('vocabularyMode').value = 'textbook';
      $('writingStatus').textContent = ready ? '' : '这个单元暂时无法生成范文和批改，可以先写作文。';
      if (!embedded) C.save(storage, C.fromChapter(catalog.find(c => c.id === id)));
      const url = new URL(location.href); url.searchParams.set('chapter', id); history.replaceState(null, '', url);
    } catch { if (version === scopeVersion) { $('unitTitle').textContent = '这个单元暂时不可用'; $('draftStatus').textContent = '请返回首页重新选择课程。'; $('writingStatus').textContent = '请返回首页重新选择课程，再开始写作。'; } }
  }
  $('publisher').onchange = () => grade();
  $('grade').onchange = () => edition();
  $('edition').onchange = () => chapter();
  $('chapter').onchange = loadScope;
  async function generate() {
    if (!ready || busy) return;
    if (!$('writingForm').reportValidity()) return;
    setBusy(true, 'generate'); $('result').hidden = true;
    $('writingStatus').textContent = '正在准备范文，请稍等…';
    try {
      const response = await fetch('/api/writing/compositions', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({chapterId:$('chapter').value, instruction:$('instruction').value,
          wordCount:Number($('wordCount').value), searchFirst:true,
          vocabularyMode:$('vocabularyMode').value === 'natural' ? 'natural' : 'textbook',
          strict:$('vocabularyMode').value === 'strict'})});
      const result = await response.json();
      if (!response.ok) {
        $('writingStatus').textContent = response.status === 400 ? '请检查写作要求和目标词数（30–300）。'
          : response.status === 429 || result.code === 'LOCAL_VOICE_BUSY' ? '猫猫正忙，请稍后再试。'
          : '这次范文没有准备好，请调整写作要求或稍后重试。';
        return;
      }
      $('essayTitle').textContent = result.title;
      $('essayBody').replaceChildren(...result.essay.split(/([A-Za-z]+(?:['’][A-Za-z]+)*)/).map(part => {
        const key = part.toLowerCase().replaceAll('’', "'");
        if (!Object.hasOwn(result.glossary || {}, key)) return document.createTextNode(part);
        const mark = document.createElement('mark'); mark.textContent = part; mark.title = result.glossary[key];
        return mark;
      }));
      const glossary = Object.entries(result.glossary || {});
      $('glossaryWords').replaceChildren(...glossary.flatMap(([word, meaning]) => {
        const term = document.createElement('dt'), detail = document.createElement('dd');
        term.textContent = word; detail.textContent = meaning; return [term, detail];
      }));
      $('essayGlossary').hidden = !glossary.length;
      $('originLabel').textContent = result.origin === 'web_reference' ? '范文节选' : result.vocabularyMode === 'natural' ? 'AI 范文 · 自然表达' : 'AI 范文 · 教材词汇';
      $('modelSource').replaceChildren(); $('modelSource').hidden = true;
      if (result.origin === 'web_reference' && result.matchedSource) {
        const link = document.createElement('a');
        link.href = result.matchedSource.url; link.textContent = '阅读完整范文';
        link.target = '_blank'; link.rel = 'noopener noreferrer';
        $('modelSource').append(link); $('modelSource').hidden = false;
      }
      $('copyEssay').textContent = result.origin === 'web_reference' ? '复制节选' : '复制作文';
      $('result').hidden = false; $('writingStatus').textContent = result.origin === 'web_reference' ? '已找到合适的网上短文，请打开来源阅读原文。' : '作文已完成，可以阅读和仿写。';
      $('result').scrollIntoView({behavior:'smooth', block:'start'});
    } catch { $('writingStatus').textContent = '范文暂时无法生成，请检查网络后重试。'; }
    finally { setBusy(false); }
  }
  $('writingForm').onsubmit = event => { event.preventDefault(); generate(); };
  $('reviewEssay').onclick = async () => {
    if (!ready || busy) return;
    if (!$('studentEssay').value.trim()) { $('draftStatus').textContent = '先写几句话，再请猫猫点评。'; $('studentEssay').focus(); return; }
    saveDraft(); setBusy(true, 'review'); $('reviewResult').hidden = true;
    $('writingStatus').textContent = '正在阅读你的作文，检查表达并整理建议…';
    try {
      const response = await fetch('/api/writing/reviews', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({chapterId:activeChapter,title:$('studentTitle').value,essay:$('studentEssay').value,
          instruction:$('instruction').value,wordCount:Number($('wordCount').value)})});
      const result = await response.json();
      if (!response.ok) {
        $('writingStatus').textContent = response.status === 400 ? '请先检查作文内容和目标词数。'
          : response.status === 429 || result.code === 'LOCAL_VOICE_BUSY' ? '猫猫正忙，请稍后再试。'
          : '这次点评没有完成，请稍后重试。';
        return;
      }
      $('reviewScore').textContent = `本次得分 ${result.totalScore}/${result.maxScore}`;
      $('reviewOverall').textContent = result.overall;
      const nodes = [];
      for (const [label, items] of [['写得好的地方',result.strengths], ['下次试试这样写',result.suggestions],
          ['具体修改建议',result.corrections.map(c => `${c.original} → ${c.suggestion}：${c.reason}`)]]) {
        if (!items.length) continue;
        const h = document.createElement('h4'), ul = document.createElement('ul'); h.textContent = label;
        for (const item of items) { const li = document.createElement('li'); li.textContent = item; ul.append(li); }
        nodes.push(h,ul);
      }
      $('reviewDetails').replaceChildren(...nodes);
      $('reviewResult').hidden = false; $('writingStatus').textContent = '点评完成。可以修改自己的草稿，再评一次。';
      $('reviewResult').scrollIntoView({behavior:'smooth', block:'start'});
    } catch { $('writingStatus').textContent = '点评暂时无法完成，请检查网络后重试。'; }
    finally { setBusy(false); }
  };
  $('copyEssay').onclick = async () => {
    try { await navigator.clipboard.writeText($('essayTitle').textContent + '\n\n' + $('essayBody').textContent); $('writingStatus').textContent = '作文已复制。'; }
    catch { $('writingStatus').textContent = '复制失败，请选择正文手动复制。'; }
  };
  async function init() {
    try {
      if (window.KittyAccount) await window.KittyAccount.ready;
      preferred = C.load(storage);
      const response = await fetch('/api/practice/catalog');
      if (!response.ok) throw new Error('教材目录读取失败，请刷新重试。');
      catalog = (await response.json()).chapters;
      if (requested && !catalog.some(c => c.id === requested)) throw new Error('链接中的教材单元不可用，请从课程页重新进入。');
      const selected = catalog.find(c => c.id === requested) || catalog.find(c => c.id === preferred.chapterId)
        || (!preferred.publisher ? catalog.find(c => c.publisher === '科普版' && parseInt(c.grade) === preferred.grade) : null);
      if (parseInt(selected?.grade || preferred.grade) === 3) $('wordCount').value = '40';
      options('publisher', [...new Set(catalog.map(c => c.publisher))], undefined, undefined, selected?.publisher || preferred.publisher);
      // Resolve all four selectors together so only the chosen scope is fetched.
      options('grade', [...new Set(catalog.filter(c => c.publisher === $('publisher').value).map(c => c.grade))]
        .sort((a,b) => parseInt(a)-parseInt(b)), undefined, undefined, selected?.grade || preferred.grade + '年级');
      options('edition', [...new Set(rows().map(c => c.edition))], undefined, C.editionLabel, selected?.edition || preferred.edition);
      $('writingFields').disabled = false; chapter(selected?.id);
    } catch { $('unitTitle').textContent = '暂时无法读取课程'; $('draftStatus').textContent = '课程读取完成后，就可以在这里写作文。'; $('writingStatus').textContent = requested ? '这个单元暂时不可用，请返回首页重新选择。' : '教材目录暂时无法读取，请刷新重试。'; }
  }
  init();
})();
