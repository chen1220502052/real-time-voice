"use strict";
const byId = (id) => document.getElementById(id);
const grade = byId("textGrade"), messages = byId("textMessages"), status = byId("textStatus");
let sessionId = `text_${crypto.randomUUID().replaceAll("-", "")}`, controller;
const C = window.KittyCourses, chapter = byId("textChapter");
let scopeReady = false, scopeVersion = 0;

async function loadScope() {
  const version = ++scopeVersion, id = chapter.value;
  scopeReady = false; reset(); busy(false);
  byId("textScope").textContent = id ? "正在读取课程…" : "用简单英语，聊聊你的日常生活。";
  if (!id) {
    scopeReady = true;
    status.textContent = "";
    busy(false);
    return;
  }
  try {
    const response = await fetch(`/api/text-bot/scopes/${encodeURIComponent(id)}`);
    const data = await response.json();
    if (version !== scopeVersion) return;
    if (!response.ok) throw Error(data.message || "词汇范围加载失败");
    byId("textScope").textContent = data.textbookReady
      ? "用这课学过的词句，和猫猫聊一聊。" : "本课资料还在整理，可以先用简单英语聊天。";
    scopeReady = true;
    grade.value = String(parseInt(data.selection.grade, 10));
    byId("textCourse").textContent = [C.gradeNames[Number(grade.value)], data.selection.publisher, C.editionLabel(data.selection.edition), data.selection.unit].join(" · ");
    const voiceLink = document.querySelector('a[href^="/voice.html"]');
    if (voiceLink) voiceLink.href = `/voice.html?chapter=${encodeURIComponent(id)}`;
    status.textContent = "";
  } catch {
    if (version !== scopeVersion) return;
    byId("textCourse").textContent = "这个单元暂时不可用";
    byId("textScope").textContent = "请返回首页重新选择课程，再开始聊天。";
    byId("textEmpty").hidden = true;
    status.textContent = "";
  }
  busy(false);
}

function bubble(role, content = "") {
  byId("textEmpty").hidden = true;
  const item = document.createElement("div");
  item.className = `text-bubble ${role}`;
  const label = document.createElement("small"), text = document.createElement("p");
  label.textContent = role === "user" ? "你" : "猫猫";
  text.textContent = content;
  item.append(label, text); messages.append(item); item.scrollIntoView({block: "nearest"});
  return text;
}
function busy(value) {
  byId('textInput').disabled = !scopeReady;
  byId("newChat").disabled = value || !scopeReady; byId("sendText").disabled = value || !scopeReady;
  byId("stopText").hidden = !value;
}
async function ask(event = "user_message", text = "") {
  if (controller || !scopeReady) return;
  controller = new AbortController(); const active = controller;
  const requestId = `req_${crypto.randomUUID().replaceAll("-", "")}`;
  let answer = "", complete = false, endConversation = false, output;
  busy(true); status.textContent = "猫猫正在想怎么接话…";
  try {
    const response = await fetch("/api/text-bot/replies", {
      method: "POST", headers: {"Content-Type": "application/json"}, signal: active.signal,
      body: JSON.stringify({version: 2, requestId, sessionId, turnId: requestId, event,
        channel: "text", text, options: {grade: Number(grade.value), ...(chapter.value ? {chapterId: chapter.value} : {})}})
    });
    if (!response.ok) {
      const error = await response.json(); throw new Error(error.message || "服务暂时不可用");
    }
    output = bubble("assistant");
    const reader = response.body.getReader(), decoder = new TextDecoder(); let pending = "";
    function accept(line) {
      if (!line.trim()) return;
      const item = JSON.parse(line);
      if (item.version !== 2 || item.requestId !== requestId) throw new Error("回复标识不匹配");
      if (complete) throw new Error("回复结束后收到额外消息");
      if (item.type === "text_delta" || item.type === "progress") { answer += item.text; output.textContent = answer; }
      else if (item.type === "done") complete = true;
      else if (item.type === "action" && item.action === "hang_up" && item.when === "after_playback") endConversation = true;
      else if (item.type === "error") throw new Error("猫猫暂时没能回复，请稍后重试。");
      else throw new Error("未知的文本 Bot 事件");
    }
    try {
      while (true) {
        const {done, value} = await reader.read();
        pending += decoder.decode(value || new Uint8Array(), {stream: !done});
        let newline;
        while ((newline = pending.indexOf("\n")) >= 0) { accept(pending.slice(0, newline)); pending = pending.slice(newline + 1); }
        if (done) { if (pending.trim()) accept(pending); break; }
      }
    } finally { reader.releaseLock(); }
    if (!complete || !answer.trim()) throw new Error("回复未完整结束，请重试");
    const receipt = await fetch("/api/text-bot/interactions", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({sessionId, interactions: [{eventId: `display_${requestId}`,
        replyId: requestId, type: "text_displayed", sequence: 1}]})
    });
    if (!receipt.ok) throw new Error("回复显示状态未保存，请重新开始聊天");
    status.textContent = endConversation ? "聊天已结束，可以开始新聊天。" : "轮到你啦。";
    if (endConversation) sessionId = `text_${crypto.randomUUID().replaceAll("-", "")}`;
  } catch (error) {
    active.abort();
    if (event === "user_message" && !byId("textInput").value) byId("textInput").value = text;
    status.textContent = error.name === "AbortError" ? "已停止回复，可以继续发送消息。" : "这次没接上，请稍后重试，或开始新聊天。";
    if (output && !answer) output.textContent = "这次没接上，请再试一次。";
    if (output) output.parentElement.classList.add("incomplete");
  } finally { controller = undefined; busy(false); }
}
function reset() {
  sessionId = `text_${crypto.randomUUID().replaceAll("-", "")}`;
  messages.replaceChildren(); byId("textEmpty").hidden = false; status.textContent = "";
}
byId("textForm").addEventListener("submit", (event) => {
  event.preventDefault(); if (controller || !scopeReady) return;
  const content = byId("textInput").value.trim(); if (!content) return;
  bubble("user", content); byId("textInput").value = "";
  ask("user_message", content);
});
byId("newChat").addEventListener("click", () => { if (!controller) { reset(); ask("session_start"); } });
byId("stopText").addEventListener("click", () => controller?.abort());
async function loadCatalog() {
  busy(false);
  try {
    if (window.KittyAccount) await window.KittyAccount.ready;
    let preferred;
    try { preferred = C.load(window.KittyAccount ? window.KittyAccount.storage : localStorage); }
    catch { preferred = C.normalize(); }
    const requested = new URL(location.href).searchParams.get("chapter");
    chapter.value = requested === null ? preferred.chapterId : requested;
    grade.value = String(preferred.grade);
    byId("textCourse").textContent = chapter.value ? "正在读取当前课程…" : `${C.gradeNames[preferred.grade]} · ${preferred.track === 'extension' ? '分级拓展' : '教材同步'}`;
    if (!chapter.value && (requested !== null || preferred.track === 'textbook')) {
      byId('textCourse').textContent = '请先选择教材单元';
      byId('textEmpty').hidden = true;
      byId("textScope").textContent = "请返回学习首页选择教材单元，再开始聊天。";
      status.textContent = "请先在首页选择教材单元。";
      return;
    }
    await loadScope();
  } catch { byId("textScope").textContent = "课程暂时无法读取，请检查网络后刷新重试。"; byId("textCourse").textContent = "当前课程暂时无法读取"; byId("textEmpty").hidden = true; }
}
loadCatalog();
(async () => {
  try {
    const response = await fetch('/health');
    if (response.ok) byId('voiceModeLink').hidden = (await response.json()).voiceEnabled !== true;
  } catch {}
})();
