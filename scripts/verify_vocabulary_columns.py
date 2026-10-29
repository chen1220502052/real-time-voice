"""Independently extract columns on every page crossing a unit boundary."""
import asyncio
import hashlib
import json
import os
import ssl
from pathlib import Path

import aiohttp
import certifi
from PIL import Image

import extract_curriculum_vocabulary as ocr


async def main():
    key = os.getenv('DEEPSEEK_API_KEY') or os.getenv('LLM_API_KEY')
    if not key:
        raise SystemExit('Existing DeepSeek credential required')
    scratch = ocr.ROOT / '.local/curriculum-column-crops'
    scratch.mkdir(parents=True,exist_ok=True)
    records = []
    for row in json.loads((ocr.DATA / 'image-manifest.json').read_text()):
        path = ocr.DATA / 'pages' / (row['sha256'][:24]+'.json')
        data = json.loads(path.read_text())
        units = {b['unit'] for b in data['passes'][-1]['blocks']}
        if len(units)>1 and any(u is not None for u in units) and not data.get('columnChecked'):
            records.append((row,path,data))
    ocr.PROMPT += '\nThis image is ONE COLUMN ONLY. Read strictly TOP TO BOTTOM. A heading applies ONLY to entries BELOW it. At the start of the column use unit=null for continuation above the first numeric heading. Never attach entries ABOVE a heading to that heading.\n'
    gate = asyncio.Semaphore(6)
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    async with aiohttp.ClientSession(connector=connector,headers={'Authorization':'Bearer '+key},
                                    timeout=aiohttp.ClientTimeout(total=150)) as client:
        async def check(record):
            row,path,data = record
            image = Image.open(ocr.ROOT / row['image'])
            columns = []
            for side,box in [('left',(0,0,image.width//2,image.height)),
                             ('right',(image.width//2,0,image.width,image.height))]:
                crop = scratch / (row['sha256'][:24]+'-'+side+'.jpg')
                image.crop(box).convert('RGB').save(crop,quality=95)
                digest = hashlib.sha256(crop.read_bytes()).hexdigest()
                item = {**row,'image':str(crop.relative_to(ocr.ROOT)),'sha256':digest,'units':[]}
                status = await ocr.extract_page(client,item,gate)
                if status == 'failed':
                    return
                extracted = json.loads((ocr.DATA/'pages'/(digest[:24]+'.json')).read_text())
                columns.append({'side':side,'sha256':digest,'passes':extracted['passes']})
            data['columnPasses'] = columns
            data['columnChecked'] = True
            path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            print('COLUMNS CHECKED',row['grade'],row['term'],Path(row['image']).name,flush=True)
        await asyncio.gather(*(check(record) for record in records))
    print('COLUMN AUDIT FINISHED',len(records),flush=True)


if __name__ == '__main__':
    asyncio.run(main())
