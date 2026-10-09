"""Local screenshot OCR. Output is a draft; no case writes."""
import json,re,sys

def parse_rows(rows):
    entries=[{'text':t,'x':sum(p[0] for p in box)/4,'y':sum(p[1] for p in box)/4} for box,t,score in rows if score>=.6]
    metrics={}; warnings=[]
    ids=[e['text'] for e in entries if re.fullmatch(r'\d{15,22}',e['text'])]
    if len(ids)>1:return {'metrics':{},'warnings':['截图包含多个视频，请裁剪为单条视频后重试。'],'raw_text':'\n'.join(e['text'] for e in entries)}
    if ids:metrics['video_id']=ids[0]
    handles=[e['text'] for e in entries if re.fullmatch(r'@[\w.]+',e['text'])]
    if len(handles)==1:metrics['creator_handle']=handles[0]
    columns=[]
    for e in entries:
        t=re.sub(r'\s','',e['text']).lower()
        key=next((k for word,k in [('sold','items_sold'),('ctor','ctor_display'),('views','views_display'),('impressions','product_impressions_display'),('clicks','product_clicks_display')] if word in t),None)
        if key:columns.append((e['x'],e['y'],key))
    columns.sort()
    for x,y,key in columns:
        candidates=[e for e in entries if e['y']>y+15 and abs(e['x']-x)<140 and re.fullmatch(r'\d[\d,.]*[KkMm%]?',e['text']) and len(e['text'])<14 and min(columns,key=lambda c:abs(c[0]-e['x']))[2]==key]
        if not candidates:continue
        e=min(candidates,key=lambda e:e['y']);value=e['text']
        if key=='items_sold':
            if not re.fullmatch(r'[\d,]+',value):continue
            value=int(value.replace(',',''))
        metrics[key]=value
    if not columns:warnings.append('未识别到表头，销售指标不自动对应，请按原图手工填写。')
    updated=next((e['text'] for e in entries if 'updated' in e['text'].lower()),'')
    return {'metrics':metrics,'updated_on':updated,'warnings':warnings,'raw_text':'\n'.join(e['text'] for e in entries)}

if __name__=='__main__':
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
    with Image.open(sys.argv[1]) as im:
        if im.width*im.height>25000000:raise ValueError('Image too large')
    rows,_=RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1)(sys.argv[1])
    print(json.dumps(parse_rows(rows or []),ensure_ascii=False))
