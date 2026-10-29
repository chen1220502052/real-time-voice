/* Original sample curriculum. Audio is synthetic, not a textbook recording. */
(function (root) {
  const unit = {
    id: 'weekend-v1', title: '我的周末计划', english: 'A little weekend adventure', grade: 6,
    subtitle: '把喜欢的活动，说给猫猫听。', source: '六年级 · 原创拓展 · 样板单元',
    words: [
      {id:'visit', word:'visit grandparents', meaning:'看望祖父母', phonetic:'/ˈvɪzɪt ˈɡrænpeərənts/', example:'I usually visit my grandparents on Sundays.', icon:'home', audio:'visit'},
      {id:'badminton', word:'play badminton', meaning:'打羽毛球', phonetic:'/pleɪ ˈbædmɪntən/', example:'I play badminton with my friends.', icon:'activity', audio:'badminton'},
      {id:'read', word:'read a book', meaning:'读一本书', phonetic:'/riːd ə bʊk/', example:'I like to read a book in the park.', icon:'book', audio:'read'},
      {id:'picnic', word:'have a picnic', meaning:'去野餐', phonetic:'/hæv ə ˈpɪknɪk/', example:'We have a picnic on Saturday.', icon:'sun', audio:'picnic'},
      {id:'usually', word:'usually', meaning:'通常', phonetic:'/ˈjuːʒuəli/', example:'I usually get up at seven.', icon:'clock', audio:'usually'},
      {id:'because', word:'because', meaning:'因为', phonetic:'/bɪˈkɒz/', example:'I like reading because it is fun.', icon:'message', audio:'because'}
    ],
    sentences: [
      {text:'What do you do on weekends?', meaning:'你周末做什么？', note:'用 What do you do…? 询问经常做的活动。', audio:'question'},
      {text:'I usually play badminton on Saturdays.', meaning:'我通常在星期六打羽毛球。', note:'I usually + 活动：介绍习惯。on 后接具体的星期。', audio:'sentence-usually'},
      {text:'I like reading because it is fun.', meaning:'我喜欢阅读，因为它很有趣。', note:'I like + 动词-ing；because 后面补充原因。', audio:'sentence-because'}
    ],
    listening: {
      audio:'dialogue', title:'听听 Mia 和 Leo 的周末',
      transcript:'Mia: What do you do on weekends, Leo?\nLeo: I usually play badminton on Saturday mornings.\nMia: That sounds fun! What about Sundays?\nLeo: I visit my grandparents on Sundays. We read books together.\nMia: I like reading too. This Saturday, I will have a picnic in the park.\nLeo: Great! What time?\nMia: At ten in the morning.',
      hint:'留意活动、星期和时间。Saturday 是星期六，Sunday 是星期日。',
      questions:[
        {id:'listen-activity', concept:'listen-activity', prompt:'Leo 通常在星期六上午做什么？', options:['看望祖父母','打羽毛球','去野餐'], answer:1, explanation:'Leo 说：“I usually play badminton on Saturday mornings.”', evidence:'I usually play badminton on Saturday mornings.'},
        {id:'listen-sunday', concept:'listen-sunday', prompt:'Leo 在星期日和谁一起读书？', options:['他的祖父母','Mia','他的老师'], answer:0, explanation:'Leo 先说星期日看望祖父母，再说“We read books together.”。', evidence:'I visit my grandparents on Sundays. We read books together.'},
        {id:'listen-time', concept:'listen-time', prompt:'Mia 的野餐几点开始？', options:['上午九点','下午两点','上午十点'], answer:2, explanation:'Mia 最后说：“At ten in the morning.”', evidence:'At ten in the morning.'}
      ]
    },
    reading: {
      title:'A different kind of Saturday', audio:'story',
      paragraphs:[
        'My name is Mia. I usually read a book at home on Saturdays. This Saturday is different. My friend Leo and I will have a picnic in the park at ten in the morning.',
        'I will bring some sandwiches and apples. Leo will bring water and a badminton set. We want to play badminton before lunch. My little brother Ben will come too. He likes drawing, so he will bring his pencils.',
        'After lunch, we will read a story under a big tree. I like the park because it is quiet and green. A good weekend does not have to be busy. Sometimes, time with friends is enough.'
      ],
      questions:[
        {id:'read-main', concept:'read-main', prompt:'这篇文章主要在讲什么？', options:['Mia 的周六野餐计划','Ben 的学校生活','怎样做三明治'], answer:0, explanation:'全文围绕本周六在公园野餐的安排展开。', paragraph:0, evidence:'My friend Leo and I will have a picnic in the park at ten in the morning.'},
        {id:'read-detail', concept:'read-detail', prompt:'谁会带水和羽毛球用具？', options:['Mia','Leo','Ben'], answer:1, explanation:'第二段写道：“Leo will bring water and a badminton set.”', paragraph:1, evidence:'Leo will bring water and a badminton set.'},
        {id:'read-reason', concept:'read-reason', prompt:'Mia 为什么喜欢这个公园？', options:['那里有很多商店','离学校很近','那里安静又绿意盎然'], answer:2, explanation:'最后一段用 because 给出原因：“it is quiet and green”。', paragraph:2, evidence:'I like the park because it is quiet and green.'}
      ]
    },
    sentenceQuestions:[
      {id:'sentence-order', concept:'sentence-order', prompt:'选择正确的句子，表达“我通常在星期六打羽毛球”。', options:['I play usually badminton on Saturdays.','I usually play badminton on Saturdays.','I usually badminton play on Saturdays.'], answer:1, explanation:'usually 放在主语 I 后、实义动词 play 前。'},
      {id:'sentence-because', concept:'because', prompt:'补全句子：I like reading ___ it is fun.', options:['because','usually','on'], answer:0, explanation:'because 表示“因为”，后面解释喜欢阅读的原因。'},
      {id:'sentence-write', concept:'sentence-expression', prompt:'用 usually 写一句你的周末活动。（例如：I usually read a book on Sundays.）', accepted:/^i\s+usually\s+(?:play badminton|read (?:a book|books)|visit my grandparents|have a picnic)(?:\s+on (?:saturdays|sundays|weekends))?[.!]?$/i, explanation:'试试：I usually read a book on Sundays. 支持本课四个活动词块；这是句型练习，不是开放写作评分。'}
    ]
  };
  unit.wordQuestions = unit.words.map((w, i) => ({id:`word-${w.id}`, concept:w.id, prompt:`“${w.word}” 是什么意思？`, options:[w.meaning, unit.words[(i+2)%6].meaning, unit.words[(i+4)%6].meaning], answer:0, explanation:w.example, audio:w.audio}));
  // Rotate correct positions so recall cannot be replaced by always choosing the first option.
  unit.wordQuestions.forEach((q,i)=>{const shift=i%3; for(let n=0;n<shift;n++)q.options.unshift(q.options.pop());q.answer=shift;});
  unit.modules = [
    {id:'words', title:'单词', english:'Words & phrases', description:'认识 6 个周末词句', minutes:4, icon:'cards', color:'mint'},
    {id:'sentences', title:'句子', english:'Make it yours', description:'用句型说出自己的想法', minutes:4, icon:'message', color:'peach'},
    {id:'listening', title:'听力', english:'Listen closely', description:'听懂 Mia 和 Leo 的对话', minutes:3, icon:'headphones', color:'lilac'},
    {id:'reading', title:'阅读', english:'Read & discover', description:'发现一个不一样的星期六', minutes:5, icon:'book', color:'blue'},
    {id:'speaking', title:'口语', english:'Your turn to speak', description:'跟读、介绍周末、和猫猫聊天', minutes:4, icon:'mic', color:'yellow'},
    {id:'review', title:'复习', english:'Keep it with you', description:'换个方式，再试一次', minutes:3, icon:'refresh', color:'mint'}
  ];
  if (typeof module !== 'undefined') module.exports = unit; else root.KittyUnit=unit;
})(typeof window !== 'undefined' ? window : globalThis);
