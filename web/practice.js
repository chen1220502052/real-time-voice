"use strict";
const el = (id) => document.getElementById(id);
const reference = el("referenceAudio");
const selfAudio = el("selfAudio");
const sentencePlayer = new window.KittySentencePlayer(el("sentenceAudio"), {
  onStart: () => { reference.pause(); el("shadowFullAudio").pause(); selfAudio.pause(); releaseRecording(); status("正在听示范…听完后，点“我来读”。"); },
  onError: () => sampleError(),
  onFinish: () => sampleFinished(),
});
let chapters = [], current = null, mode = "shadow", loadVersion = 0;
let recorder = null, stream = null, recordingUrl = null;
let recordingVersion = 0, recordingTimer = null, requestingMic = false;
let practiceTarget = null, targetButtons = [], wordButtons = [];
let shadowWordButtons = [];
let shadowGroups = [], shadowGroup = 0, shadowIndex = 0, shadowChapter = null, shadowKind = null;
let sampleState = "idle", sampleHeard = false, sampleAudio = null, sampleAttempt = 0, sampleTargetKey = null;
let preferredCourse = {};
let courseStorage;
try { courseStorage = (window.KittyAccount ? window.KittyAccount.storage : window.localStorage); } catch { courseStorage = null; }
function keepValue(id, wanted) {
  const select = el(id);
  if ([...select.options].some(option => option.value === String(wanted))) select.value = String(wanted);
}
function rememberChapter() {
  if (!current) return;
  if (courseStorage && !window.KittyCourses.save(courseStorage, window.KittyCourses.fromChapter(current))) status("课程选择暂时无法保存，请检查同步状态。");
  const speakingLink = el("chapterSpeakingLink");
  if (speakingLink) speakingLink.href = window.KittyCourses.practiceURL(window.KittyCourses.fromChapter(current), "speaking");
  const url = new URL(location.href);
  url.searchParams.set("chapter", current.id);
  url.searchParams.set("mode", mode);
  if (mode === "shadow") url.searchParams.set("kind", el("kind").value);
  else url.searchParams.delete("kind");
  history.replaceState(null, "", url);
}

function status(text) { el("practiceStatus").textContent = text; }
function options(id, rows, value, label = value) {
  const select = el(id);
  select.replaceChildren(...rows.map((row) => new Option(label(row), value(row))));
  select.disabled = !rows.length;
}
function values(rows, key) { return [...new Set(rows.map((row) => row[key]))]; }
function stopReference() {
  sampleAttempt++; sampleAudio = null;
  reference.pause(); sentencePlayer.stop(); el("shadowFullAudio").pause();
  setSampleState(sampleHeard ? "finished" : "idle");
}
function releaseRecording(clear = false) {
  clearTimeout(recordingTimer);
  if (clear || requestingMic) recordingVersion++;
  if (recorder && recorder.state !== "inactive") recorder.stop();
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  requestingMic = false;
  el("recordSelf").disabled = !current || (mode === "shadow" && (!practiceTarget?.source || Boolean(practiceTarget.sampleFailed)));
  el("recordSelf").textContent = recordingUrl ? "再读一次" : (mode === "shadow" ? "2. 我来读" : "开始录音");
  el("stopRecording").disabled = true;
  if (clear) {
    selfAudio.pause();
    selfAudio.removeAttribute("src");
    selfAudio.load();
    selfAudio.hidden = true;
    if (recordingUrl) URL.revokeObjectURL(recordingUrl);
    recordingUrl = null;
    el("playSelf").hidden = mode !== "shadow";
    el("playSelf").disabled = true;
    el("recordSelf").textContent = mode === "shadow" ? "2. 我来读" : "开始录音";
  }
  for (const button of targetButtons) button.disabled = requestingMic || recorder?.state === "recording";
  lockShadowControls(false);
}
function resetPractice() {
  stopReference(); releaseRecording(true);
  el("listeningNotes").value = "";
  el("speakingTask").querySelector("details").open = false;
  el("wordSupplement").open = false;
}
function choosePublisher() {
  const wantedGrade = el("grade").value || preferredCourse.grade + "年级";
  const rows = chapters.filter((row) => row.publisher === el("publisher").value);
  options("grade", values(rows, "grade").sort((a, b) => parseInt(a) - parseInt(b)), (x) => x);
  keepValue("grade", wantedGrade);
  chooseGrade();
}
function chooseGrade() {
  const wantedEdition = el("edition").value || preferredCourse.edition;
  const rows = chapters.filter((row) => row.publisher === el("publisher").value && row.grade === el("grade").value);
  options("edition", values(rows, "edition"), (x) => x, window.KittyCourses.editionLabel);
  keepValue("edition", wantedEdition);
  chooseEdition();
}
function chooseEdition() {
  const wantedChapter = el("chapter").value || preferredCourse.chapterId;
  const rows = chapters.filter((row) => row.publisher === el("publisher").value && row.grade === el("grade").value && row.edition === el("edition").value);
  options("chapter", rows, (row) => row.id, (row) => row.unit);
  keepValue("chapter", wantedChapter);
  loadChapter();
}
async function loadChapter() {
  const version = ++loadVersion;
  resetPractice();
  current = null;
  render();
  el("chapterTitle").textContent = "正在加载章节…";
  if (!el("chapter").value) { status("暂无章节资料，请先配置教材资源。"); return; }
  try {
    const response = await fetch(`/api/practice/chapters/${encodeURIComponent(el("chapter").value)}`);
    if (!response.ok) throw new Error("chapter");
    const item = await response.json();
    if (version !== loadVersion) return;
    current = item;
    el("coursePickerSummary").textContent = `${item.grade} · ${item.unit} · 换一课`;
    el("coursePickerDetails").open = !window.matchMedia?.("(max-width: 760px)").matches;
    rememberChapter();
    el("chapterTitle").textContent = item.unit;
    el("speakingQuestion").textContent = item.question;
    el("speakingExample").textContent = item.example;
    el('wordSourceNotice').textContent = item.curriculum ? '教材原图识别并复核的单元词表；原图可在作文页的词汇范围中查看。' : (item.vocabulary.length ? '原创主题词补充，不是教材完整词表。' : '本课词表尚未完成结构化核验，请先使用教材原图和原音练习。');
    el("speakingTasks").replaceChildren(...item.tasks.filter((task) => !/跟读|听音/.test(task.type)).map((task) => {
      const li = document.createElement("li"); li.textContent = task.instruction; return li;
    }));
    wordButtons = [];
    el("words").replaceChildren(...item.vocabulary.map((word) => {
      const card = document.createElement("article"); card.className = "word-practice";
      const span = document.createElement("span"); span.textContent = `${word.word} · ${word.meaning}`;
      const button = document.createElement("button"); button.type = "button"; button.textContent = "跟读";
      button.setAttribute("aria-label", `跟读单词：${word.word}`);
      button.addEventListener("click", () => startTarget({key: `word:${word.word}`, label: word.word, source: null}));
      wordButtons.push(button);
      card.append(span, button); return card;
    }));
    render(); status("已切换到本章，可以开始练习。");
  } catch {
    if (version === loadVersion) {
      el("chapterTitle").textContent = "章节暂时无法加载";
      status("读取章节失败，请重新选择章节或刷新页面重试。");
    }
  }
}
function render() {
  stopReference();
  targetButtons = [...wordButtons];
  el("kindLabel").hidden = mode !== "shadow";
  el("audioPractice").hidden = mode !== "listening" || !current;
  el("listeningTask").hidden = mode !== "listening" || !current;
  el("speakingTask").hidden = mode !== "speaking" || !current;
  el("wordSupplement").hidden = mode !== "speaking" || !current;
  el('selfRecordSection').hidden = mode === 'writing' || !current;
  el('writingTask').hidden = mode !== 'writing' || !current;
  if (mode === 'writing' && current) {
    const target = '/writing.html?embedded=1&chapter=' + encodeURIComponent(current.id);
    if (el('writingFrame').getAttribute('src') !== target) el('writingFrame').setAttribute('src', target);
    el('writingFullLink').href = '/writing.html?chapter=' + encodeURIComponent(current.id);
  }
  el("recordSelf").disabled = !current || (mode === "shadow" && (!practiceTarget?.source || Boolean(practiceTarget.sampleFailed)));
  document.querySelectorAll("[data-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === mode)));
  el("instruction").textContent = {
    shadow: "先听一个词或一小段，再录下自己的声音，来回听听哪里不一样。",
    listening: "先不看课本听一遍，再记录你听到的内容。需要时重听，最后翻开课本核对。",
    speaking: "围绕本章主题回答问题，再完成拓展表达。示范可以帮助你组织自己的话。",
    writing: '先自己写，再看看点评；也可以先搜网上范文，或生成符合本单元词汇范围的原创范文。',
  }[mode];
  const playable = current?.recordings.filter((item) => item.url) || [];
  const hasWords = Boolean(current?.vocabulary?.length);
  const availableKinds = new Set(playable.map(item => item.kind));
  if (hasWords) availableKinds.add("words");
  if (current?.textbookPages?.length) availableKinds.add("passage");
  el("kindLabel").hidden = mode !== "shadow" || !availableKinds.size;
  for (const option of el("kind").options) option.disabled = !availableKinds.has(option.value);
  if (mode === "shadow" && availableKinds.size && !availableKinds.has(el("kind").value)) {
    el("kind").value = availableKinds.has("passage") ? "passage" : "words";
  }
  for (const [kind, id] of [["words", "practiceWords"], ["passage", "practicePassage"]]) {
    el(id).disabled = !availableKinds.has(kind);
    el(id).setAttribute("aria-pressed", String(el("kind").value === kind));
  }
  const kind = mode === "listening" ? "passage" : el("kind").value;
  if (mode === "shadow" && kind === "words" && hasWords) {
    el("wordSupplement").open = false;
  }
  const rows = playable.filter((item) => item.kind === kind);
  options("recording", rows, (item) => item.id, (item) => item.title);
  if (mode === "shadow" && kind === "passage") {
    const scenePage = current?.textbookPages?.find(page => page.scenes?.length && rows.some(row => row.id === page.recordingId));
    if (scenePage) keepValue("recording", scenePage.recordingId);
  }
  selectRecording();
  renderShadow();
}
function selectRecording() {
  stopReference();
  // A new reference must not retain a recording made against the old one.
  releaseRecording(true);
  const item = current?.recordings.find((row) => row.id === el("recording").value);
  reference.removeAttribute("src");
  if (item?.url) reference.src = item.url;
  reference.load(); reference.playbackRate = Number(el("speed").value);
  reference.hidden = !item?.url;
  el("speed").disabled = !item?.url;
  el("recordingPicker").hidden = !item?.url;
  el("audioPlaybackControls").hidden = !item?.url;
  const wordPractice = mode === "shadow" && el("kind").value === "words";
  practiceTarget = {key: `material:${item?.id || el("kind").value}`, label: item?.title || (wordPractice ? "本章单词" : "本章朗读"), source: item?.url ? {url: item.url} : null};
  updateTarget();
  renderTextbook(item);
  el("audioNotice").textContent = item?.url
    && wordPractice ? `官方单词录音 · ${item.availability === "local" ? "本地可播放" : "在线播放，需联网"}。可对照下方词表跟读。`
    : item?.url
    ? `官方教材录音 · ${item.availability === "local" ? "本地可播放" : "在线播放，需联网"}。${current?.textbookPages?.some(page => page.recordingId === item.id && page.scenes?.length) ? "点下方每句话后的 ▶，只听这一句。" : ""}`
    : "";
  el("audioNotice").hidden = !el("audioNotice").textContent;
}
function renderTextbook(recording) {
  const allPages = current?.textbookPages || [];
  const visible = mode === "shadow" && el("kind").value === "passage" && Boolean(allPages.length);
  el("textbookReader").hidden = !visible;
  const pairedPages = recording ? allPages.filter(page => page.recordingId === recording.id) : [];
  const pages = pairedPages.length ? pairedPages : allPages;
  el("textbookNotice").textContent = !visible
    ? ""
    : pages.some(page => page.scenes?.length) ? "点击 ▶ 听这一句原音；点击“跟读”开始录音，结束后回听并与原音对照。"
    : pairedPages.length ? "点击播放器听读，点击图片可放大查看。"
    : "以下为本章教材原图，保留课本的插图和对话。可选择图片下方的分段录音，点击图片可放大查看。";
  el("textbookNotice").hidden = !el("textbookNotice").textContent;
  el("textbookPages").replaceChildren(...(visible ? pages : []).map(page => {
    if (page.scenes?.length) return renderDialogueScene(page);
    const figure = document.createElement("figure");
    figure.className = "textbook-page";
    const link = document.createElement("a");
    link.href = page.url; link.target = "_blank"; link.rel = "noopener";
    link.setAttribute("aria-label", `${page.label}，打开大图`);
    const img = document.createElement("img");
    img.src = page.url; img.alt = `${current.unit} · ${page.label}`;
    img.loading = "lazy"; img.decoding = "async";
    img.addEventListener("error", () => { img.hidden = true; link.textContent = "图片加载失败，点击重试查看原图"; });
    link.append(img);
    const caption = document.createElement("figcaption");
    const label = document.createElement("span"); label.textContent = page.label;
    caption.append(label);
    const paired = current.recordings.find(item => item.id === page.recordingId && item.url);
    if (paired) {
      const button = document.createElement("button"); button.type = "button";
      button.textContent = "听这一页";
      button.addEventListener("click", () => {
        el("recording").value = paired.id; selectRecording();
        reference.play().catch(() => status("请点击播放器的播放按钮开始听读。"));
      });
      caption.append(button);
    }
    const follow = document.createElement("button"); follow.type = "button"; follow.textContent = "跟读这一页";
    follow.addEventListener("click", () => startTarget({key: `page:${page.id}`, label: page.label,
      source: paired ? {url: paired.url} : null}));
    targetButtons.push(follow); caption.append(follow);
    figure.append(link, caption); return figure;
  }));
}
function renderDialogueScene(page) {
  const group = document.createElement("section"); group.className = "dialogue-scenes";
  const recording = current.recordings.find(item => item.id === page.recordingId);
  for (const scene of page.scenes) {
    const card = document.createElement("section"); card.className = "dialogue-scene-card";
    const title = document.createElement("h4"); title.textContent = scene.title;
    const stage = document.createElement("div"); stage.className = "dialogue-scene";
    if (page.backgroundUrl) stage.className += " is-clean";
    stage.setAttribute("aria-label", scene.title);
    const [iw, ih] = page.imageSize, [x, y, w, h] = scene.crop;
    stage.style.aspectRatio = `${w} / ${h}`;
    stage.style.backgroundImage = `url("${page.backgroundUrl || page.url}")`;
    stage.style.backgroundSize = `${iw / w * 100}% ${ih / h * 100}%`;
    stage.style.backgroundPosition = `${iw === w ? 0 : x / (iw - w) * 100}% ${ih === h ? 0 : y / (ih - h) * 100}%`;
    stage.style.setProperty("--scene-image", stage.style.backgroundImage);
    stage.style.setProperty("--scene-size", stage.style.backgroundSize);
    stage.style.setProperty("--scene-position", stage.style.backgroundPosition);
    stage.style.setProperty("--scene-ratio", `${w} / ${h}`);
    for (const line of scene.lines) {
      const bubble = document.createElement("div"); bubble.className = "sentence-bubble";
      const [left, top, width] = line.bubble;
      bubble.style.left = `${left}%`; bubble.style.top = `${top}%`; bubble.style.width = `${width}%`;
      if (line.minHeight && !page.backgroundUrl) bubble.style.minHeight = `${line.minHeight}%`;
      const text = document.createElement("span"); text.textContent = line.text; text.lang = "en";
      const button = document.createElement("button"); button.type = "button"; button.className = "sentence-play";
      button.textContent = "▶"; button.setAttribute("aria-label", `播放：${line.text}`);
      button.setAttribute("aria-pressed", "false"); button.disabled = !recording?.url;
      button.addEventListener("click", () => {
        el("sentenceAudio").playbackRate = Number(el("speed").value);
        sentencePlayer.play({url: recording.url, start: line.start, end: line.end}, button);
      });
      const follow = document.createElement("button"); follow.type = "button"; follow.className = "sentence-follow";
      follow.textContent = "跟读"; follow.setAttribute("aria-label", `跟读句子：${line.text}`);
      follow.addEventListener("click", () => startTarget({key: `line:${page.id}:${line.start}`, label: line.text,
        source: recording?.url ? {url: recording.url, start: line.start, end: line.end} : null}));
      targetButtons.push(follow);
      bubble.append(text, button, follow); stage.append(bubble);
    }
    card.append(title, stage); group.append(card);
  }
  return group;
}
function lockShadowControls(locked) {
  for (const button of shadowWordButtons) button.disabled = locked;
  el("shadowLesson").disabled = locked || !shadowGroups.length;
  el("shadowPrevious").disabled = locked || shadowIndex === 0;
  el("shadowNext").disabled = locked || shadowIndex >= (shadowGroups[shadowGroup]?.targets.length || 0) - 1;
  el("playOriginal").disabled = locked;
}
function renderShadow() {
  const visible = mode === "shadow" && Boolean(current);
  el("shadowPractice").hidden = !visible;
  (visible ? el("shadowRecorderSlot") : el("generalRecorderSlot")).append(el("selfRecordSection"));
  el("recorderHeading").hidden = visible;
  renderSampleControl();
  el("playSelf").textContent = visible ? "3. 听我的录音" : "听我的录音";
  if (!visible) return;
  const kind = el("kind").value;
  if (shadowChapter !== current.id || shadowKind !== kind) {
    shadowChapter = current.id; shadowKind = kind; shadowGroup = 0; shadowIndex = 0;
  }
  shadowGroups = window.KittyShadowContent.groups(current, kind);
  options("shadowLesson", shadowGroups, group => group.id, group => `${group.label} · ${group.targets.length}${kind === "words" ? " 个词" : " 小段"}`);
  el("shadowLessonLabel").hidden = kind === "words" || !shadowGroups.length;
  const full = current.recordings.filter(r => r.url && r.kind === "passage" && r.fullLesson);
  el("shadowFullLesson").hidden = kind === "words" || !full.length;
  options("shadowFullSelect", full, r => r.id, r => `完整课文 · ${Math.floor(r.duration / 60)} 分 ${Math.round(r.duration % 60)} 秒`);
  if (full.length) el("shadowFullAudio").src = full[0].url;
  showShadowTarget();
}
function showShadowTarget() {
  stopReference(); releaseRecording(true);
  const group = shadowGroups[shadowGroup];
  const target = group?.targets[shadowIndex];
  practiceTarget = target || null;
  el("shadowEmpty").hidden = Boolean(target);
  el("shadowTargetCard").hidden = !target;
  el("selfRecordSection").hidden = !target;
  el("shadowWordChoices").hidden = el("kind").value !== "words";
  shadowWordButtons = [];
  el("shadowWordChoices").replaceChildren(...(el("kind").value === "words" ? group?.targets || [] : []).map((word,i) => {
    const button = document.createElement("button"); button.type = "button"; button.textContent = word.label;
    button.setAttribute("aria-pressed",String(i === shadowIndex));
    button.addEventListener("click", () => { shadowIndex = i; showShadowTarget(); }); shadowWordButtons.push(button); return button;
  }));
  if (!target) { updateTarget(); return; }
  keepValue("shadowLesson", group.id);
  el("shadowProgress").textContent = `${target.type === "word" ? "单词" : "小段"} ${shadowIndex + 1} / ${group.targets.length}`;
  el("shadowText").textContent = target.type === "excerpt" ? group.label : target.label;
  el("shadowText").lang = target.type === "excerpt" ? "zh-CN" : "en";
  el("shadowMeaning").textContent = target.meaning || (target.type === "excerpt" ? "看着下面的课文，每次只读这一小段。" : "听听这句话，再自己读一遍。");
  el("shadowSource").textContent = target.source ? `${target.sourceLabel} · ${window.KittyShadowContent.seconds(target.duration || 1)}` : "这个词的示范音暂时不可用，先练其他词吧。";
  el("shadowPrevious").disabled = shadowIndex === 0;
  el("shadowNext").disabled = shadowIndex === group.targets.length - 1;
  el("shadowPrevious").textContent = target.type === "word" ? "上一个词" : "上一小段";
  el("shadowNext").textContent = target.type === "word" ? "下一个词 →" : "下一小段 →";
  el("shadowPageDetails").hidden = !target.page;
  el("shadowPageDetails").open = target.type === "excerpt";
  if (target.page) el("shadowPage").src = target.page.url;
  else el("shadowPage").removeAttribute("src");
  el("playSelf").hidden = false; el("playSelf").disabled = true;
  updateTarget();
}
el("shadowLesson").addEventListener("change", () => {
  shadowGroup = shadowGroups.findIndex(group => group.id === el("shadowLesson").value);
  shadowIndex = 0; showShadowTarget();
});
el("shadowPrevious").addEventListener("click", () => { if (shadowIndex > 0) { shadowIndex--; showShadowTarget(); } });
el("shadowNext").addEventListener("click", () => { if (shadowIndex + 1 < (shadowGroups[shadowGroup]?.targets.length || 0)) { shadowIndex++; showShadowTarget(); } });
el("shadowFullSelect").addEventListener("change", () => {
  stopReference(); const full = current?.recordings.find(r => r.id === el("shadowFullSelect").value);
  if (full) { el("shadowFullAudio").src = full.url; el("shadowFullAudio").load(); }
});
el("shadowFullAudio").addEventListener("play", () => { reference.pause(); sentencePlayer.stop(); selfAudio.pause(); releaseRecording(); });
el("publisher").addEventListener("change", choosePublisher);
el("grade").addEventListener("change", chooseGrade);
el("edition").addEventListener("change", chooseEdition);
el("chapter").addEventListener("change", loadChapter);
function choosePracticeKind(kind) {
  if (mode !== "shadow" || el("kind").value === kind
      || [...el("kind").options].find(option => option.value === kind)?.disabled) return;
  resetPractice(); el("kind").value = kind; render(); rememberChapter();
  status(kind === "words" ? "已切换到单词跟读。" : "已切换到课文跟读。");
}
el("practiceWords").addEventListener("click", () => choosePracticeKind("words"));
el("practicePassage").addEventListener("click", () => choosePracticeKind("passage"));
el("kind").addEventListener("change", () => { resetPractice(); render(); rememberChapter(); });
el("recording").addEventListener("change", selectRecording);
document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  if (mode === button.dataset.mode) return;
  resetPractice(); mode = button.dataset.mode; rememberChapter(); render(); status("练习模式已切换。");
}));
el("speed").addEventListener("change", () => {
  reference.playbackRate = Number(el("speed").value);
  el("sentenceAudio").playbackRate = Number(el("speed").value);
});
reference.addEventListener("error", () => {
  if (!reference.getAttribute("src")) return;
  el("speed").disabled = true;
  if (mode === "shadow") sampleError();
  else status("教材录音没能打开，请重新选择或稍后再试。");
});
function sampleError() {
  if (mode === "shadow" && practiceTarget) { practiceTarget.sampleFailed = true; el("recordSelf").disabled = true; }
  setSampleState("error");
  status("发音没能播放，请点“重新播放”再试一次。");
}
function sampleReady(event) {
  if (sampleAudio && (!event?.target || event.target === sampleAudio)) setSampleState("playing");
  if (mode === "shadow" && practiceTarget?.source) { practiceTarget.sampleFailed = false; el("recordSelf").disabled = false; }
}
reference.addEventListener("playing", sampleReady);
el("sentenceAudio").addEventListener("playing", sampleReady);
reference.addEventListener("ended", () => { if (sampleAudio === reference) sampleFinished(); });
for (const audio of [reference, el("sentenceAudio")]) {
  audio.addEventListener("waiting", () => { if (sampleAudio === audio && sampleState === "playing") setSampleState("loading"); });
  audio.addEventListener("pause", () => {
    if (sampleAudio === audio && sampleState === "playing" && audio.paused) setSampleState(sampleHeard ? "finished" : "idle");
  });
}
function sampleFinished() {
  sampleHeard = true; setSampleState("finished");
  status("听完了，点“我来读”，试着读一遍吧。");
}
function setSampleState(state) { sampleState = state; renderSampleControl(); }
function renderSampleControl() {
  const word = mode === "shadow" && practiceTarget?.type === "word";
  const subject = word ? "单词发音" : "课文原音";
  const duration = mode === "shadow" && practiceTarget?.duration ? ` · ${window.KittyShadowContent.seconds(practiceTarget.duration)}` : "";
  const button = el("playOriginal");
  button.textContent = sampleState === "loading" ? "… 加载中 · 点击取消"
    : sampleState === "playing" ? "■ 正在播放 · 点击停止"
    : sampleState === "error" ? "▶ 重新播放"
    : sampleHeard ? `▶ 再听一次${duration}` : `▶ 听${mode === "shadow" ? subject : "原音"}${duration}`;
  button.dataset.playback = sampleState;
  button.setAttribute("aria-pressed", String(sampleState === "loading" || sampleState === "playing"));
  button.setAttribute("aria-busy", String(sampleState === "loading"));
  const notice = el("samplePlaybackStatus");
  notice.hidden = !practiceTarget?.source;
  notice.textContent = sampleState === "loading" ? `正在加载${subject}…再点按钮可以取消。`
    : sampleState === "playing" ? `正在播放${subject}，听听它怎么读。`
    : sampleState === "error" ? "没能播放出声音，请点“重新播放”再试一次。"
    : sampleHeard ? "播放完了，轮到你读啦！也可以再听一次。"
    : word ? "点击播放，听听这个词怎么读。" : "点击播放，听听这一小段怎么读。";
}
reference.addEventListener("play", () => { sentencePlayer.stop(); el("shadowFullAudio").pause(); selfAudio.pause(); releaseRecording(); });
selfAudio.addEventListener("play", () => { stopReference(); releaseRecording(); });
function updateTarget() {
  const key = `${mode}:${practiceTarget?.key || ""}`;
  if (sampleTargetKey !== key) {
    sampleTargetKey = key; sampleHeard = false; sampleAudio = null; sampleAttempt++; sampleState = "idle";
  }
  renderSampleControl();
  el("recordSelf").disabled = !current || (mode === "shadow" && (!practiceTarget?.source || Boolean(practiceTarget.sampleFailed)));
  el("recordTarget").hidden = mode === "shadow";
  el("targetNotice").hidden = mode === "shadow";
  el("recordTarget").textContent = practiceTarget ? `当前跟读：${practiceTarget.label}` : "选择内容，开始跟读";
  el("playOriginal").hidden = !practiceTarget?.source;
  el("targetNotice").textContent = practiceTarget?.source ? "先听原音 → 跟读录音 → 回听对照。可重复练习。" : "当前内容暂无独立原音，可以录下朗读并回听。";
}
function startTarget(target) {
  if (requestingMic || recorder?.state === "recording") return;
  if (practiceTarget?.key !== target.key) { stopReference(); releaseRecording(true); }
  practiceTarget = target; updateTarget();
  el("selfRecordSection").scrollIntoView?.({behavior: "smooth", block: "center"});
  return startRecording();
}
el("playOriginal").addEventListener("click", () => {
  const source = practiceTarget?.source;
  if (!source) return;
  if (sampleState === "loading" || sampleState === "playing") {
    stopReference(); return;
  }
  stopReference(); selfAudio.pause(); releaseRecording();
  const attempt = ++sampleAttempt;
  sampleAudio = Number.isFinite(source.start) ? el("sentenceAudio") : reference;
  setSampleState("loading");
  if (Number.isFinite(source.start)) {
    sampleAudio.playbackRate = Number(el("speed").value);
    sentencePlayer.play(source, el("compareClip"));
  } else {
    reference.src = source.url; reference.currentTime = 0;
    reference.play().catch(() => { if (attempt === sampleAttempt) sampleError(); });
  }
});
el("playSelf").addEventListener("click", () => {
  if (!recordingUrl) return;
  stopReference(); selfAudio.currentTime = 0;
  selfAudio.play().catch(() => status("录音暂时无法播放，请重试。"));
});
el("recordSelf").addEventListener("click", () => startRecording());
async function startRecording() {
  if (requestingMic || recorder?.state === "recording" || !current || (mode === "shadow" && (!practiceTarget?.source || Boolean(practiceTarget.sampleFailed)))) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    status("当前浏览器不支持录音，请使用支持录音的浏览器，并通过 HTTPS 或本机地址访问。"); return;
  }
  stopReference(); releaseRecording(true);
  const version = recordingVersion;
  requestingMic = true; el("recordSelf").disabled = true;
  for (const button of targetButtons) button.disabled = true;
  lockShadowControls(true);
  status("正在请求麦克风权限…");
  try {
    const acquired = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (version !== recordingVersion || document.hidden) { acquired.getTracks().forEach((track) => track.stop()); return; }
    stream = acquired;
    const chunks = [], active = new MediaRecorder(acquired);
    recorder = active;
    active.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
    active.addEventListener("stop", () => {
      if (version !== recordingVersion) return;
      if (!chunks.length) { status("没有录到声音，请重新跟读。"); return; }
      if (recordingUrl) URL.revokeObjectURL(recordingUrl);
      recordingUrl = URL.createObjectURL(new Blob(chunks, { type: active.mimeType }));
      selfAudio.src = recordingUrl; selfAudio.hidden = false;
      el("playSelf").hidden = false;
      el("playSelf").disabled = false;
      el("recordSelf").textContent = "再读一次";
      status("录好了！听听自己的声音，再听示范，比较一下。可以再读一次，也可以练下一个。");
    });
    active.addEventListener("error", () => { if (version === recordingVersion) { releaseRecording(true); status("录音中断，请检查麦克风后重试。"); } });
    active.start(); requestingMic = false;
    el("stopRecording").disabled = false;
    el("recordSelf").textContent = "正在跟读录音…";
    recordingTimer = setTimeout(() => releaseRecording(), mode === "shadow" ? (practiceTarget?.type === "word" ? 10000 : 30000) : 120000);
    status("正在跟读录音…说完后点击“结束跟读”。");
  } catch {
    if (version === recordingVersion) { releaseRecording(true); status("无法使用麦克风，请检查浏览器权限后重试。"); }
  }
}
el("stopRecording").addEventListener("click", () => releaseRecording());
document.addEventListener("visibilitychange", () => { if (document.hidden) { stopReference(); releaseRecording(true); } });
window.addEventListener("pagehide", () => { stopReference(); releaseRecording(true); });
async function initialize() {
  try { await window.KittyAccount?.ready; }
  catch { status("学习档案读取失败，请检查网络后刷新重试。"); return; }
  el("recordSelf").disabled = true;
  try {
    const response = await fetch("/api/practice/catalog");
    if (!response.ok) throw new Error("catalog");
    chapters = (await response.json()).chapters;
    const params = new URLSearchParams(location.search);
    const requested = params.get("chapter");
    if (["words", "passage"].includes(params.get("kind"))) el("kind").value = params.get("kind");
    const saved = courseStorage ? window.KittyCourses.load(courseStorage) : {};
    const chapter = requested ? chapters.find(row => row.id === requested) :
      (saved.track === "textbook" ? window.KittyCourses.resolve(chapters, saved) : null);
    if (requested && !chapter) {
      el("chapterTitle").textContent = "所选单元暂不可用";
      status("这个单元不在当前目录中。请返回学习首页重新选择课程。");
      return;
    }
    preferredCourse = chapter ? window.KittyCourses.fromChapter(chapter) : saved;
    mode = ["shadow", "listening", "speaking", "writing"].includes(params.get("mode")) ? params.get("mode") : "shadow";
    options("publisher", values(chapters, "publisher"), (x) => x);
    keepValue("publisher", preferredCourse.publisher);
    choosePublisher();
  } catch { el("chapterTitle").textContent = "教材暂时无法加载"; status("读取教材目录失败，请刷新页面重试。"); }
}
initialize();
