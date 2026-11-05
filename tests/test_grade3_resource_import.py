from pathlib import Path

import pytest

from scripts.import_grade3_resources import LessonPage, retrieve


def test_lesson_parser_pairs_images_with_their_segment_and_keeps_full_audio():
    page = LessonPage()
    page.feed('''<h3 class="title">Unit 1&nbsp; Hello<button>下载</button></h3>
    <a download href="http://qiniu.xdstudy.com/all.mp3"></a>
    <span><img src="http://qiniu.xdstudy.com/blank-thumbnail.jpg"></span>
    <audio src="http://qiniu.xdstudy.com/one.mp3"></audio>
    <div class="lesson-img"><p><img src="http://qiniu.xdstudy.com/page1.jpg"></p></div>
    <img src="/static/logo.jpg">
    <audio src="http://qiniu.xdstudy.com/two.mp3"></audio>
    <div class="lesson-img"><img src="http://qiniu.xdstudy.com/page2.jpg"></div>''')
    assert page.title == 'Unit 1\xa0 Hello'
    assert [a['kind'] for a in page.assets] == ['整课音频', '分段音频', '教材原图', '分段音频', '教材原图']
    assert page.assets[2]['paired_url'].endswith('/one.mp3')
    assert page.assets[4]['paired_url'].endswith('/two.mp3')


def test_resource_download_rejects_unrelated_hosts_and_local_files(tmp_path):
    for url in ['file:///etc/passwd', 'http://127.0.0.1/private', 'https://qiniu.xdstudy.com.evil.test/a.mp3']:
        with pytest.raises(ValueError, match='allowlist'):
            retrieve(url, tmp_path)
