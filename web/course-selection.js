/* Course preference is separate from the original sample's learning evidence. */
(function (root) {
  const KEY = 'kitty-course-selection:v1';
  const grades = [3, 4, 5, 6, 7, 8, 9];
  const gradeNames = {3:'三年级',4:'四年级',5:'五年级',6:'六年级',7:'七年级',8:'八年级',9:'九年级'};
  const editionLabel = value => String(value).replace(/^(上册|下册)_官方当前目录_版次待与学校核对$/, '$1').replaceAll('_', ' · ');
  function normalize(value = {}) {
    const grade = grades.includes(Number(value.grade)) ? Number(value.grade) : 6;
    const track = value.track === 'textbook' ? 'textbook' : 'extension';
    const text = key => typeof value[key] === 'string' ? value[key].slice(0,200) : '';
    return {version:1, grade, track, unitId:track === 'extension' && grade === 6 ? 'weekend-v1' : '',
      publisher:track === 'textbook' ? text('publisher') : '',
      edition:track === 'textbook' ? text('edition') : '',
      chapterId:track === 'textbook' ? text('chapterId') : ''};
  }
  function load(storage) {
    try { const value=JSON.parse(storage.getItem(KEY));return normalize(value?.version===1?value:{}); }
    catch { return normalize(); }
  }
  function save(storage,value) {
    try { storage.setItem(KEY,JSON.stringify(normalize(value)));return true; }
    catch { return false; }
  }
  function rows(catalog, choice) {
    return catalog.filter(c=>parseInt(c.grade,10)===choice.grade &&
      (!choice.publisher || c.publisher===choice.publisher) && (!choice.edition || c.edition===choice.edition));
  }
  function resolve(catalog,choice) {
    return rows(catalog,choice).find(c=>c.id===choice.chapterId) || null;
  }
  function fromChapter(chapter) {
    return normalize({grade:parseInt(chapter.grade,10),track:'textbook',publisher:chapter.publisher,
      edition:chapter.edition,chapterId:chapter.id});
  }
  function practiceURL(choice,mode='shadow') {
    const params=new URLSearchParams();
    if(choice.chapterId)params.set('chapter',choice.chapterId);
    params.set('mode',['shadow','listening','speaking','writing'].includes(mode)?mode:'shadow');
    return '/practice.html?'+params.toString();
  }
  function chatURL(choice, channel='text') {
    const params = new URLSearchParams();
    if (choice.track === 'textbook' && choice.chapterId) params.set('chapter', choice.chapterId);
    return (channel === 'voice' ? '/voice.html' : '/text.html') + (params.size ? '?' + params.toString() : '');
  }
  const api={KEY,grades,gradeNames,editionLabel,normalize,load,save,rows,resolve,fromChapter,practiceURL,chatURL};
  if(typeof module!=='undefined')module.exports=api;else root.KittyCourses=api;
})(typeof window!=='undefined'?window:globalThis);
