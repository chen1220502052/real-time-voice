"""Real macOS SafariDriver cookie/WS checks; does not run local voice services."""
import argparse
import base64
import json
from pathlib import Path
import urllib.request
import urllib.error


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--auth-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--driver-url',default='http://127.0.0.1:4447')
    args=parser.parse_args()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def command(method,path,data=None):
        request=urllib.request.Request(args.driver_url+path,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json'},method=method)
        try:
            with opener.open(request,timeout=45) as response:result=json.load(response)
        except urllib.error.HTTPError as error:
            result=json.load(error)
        value=result.get('value',{})
        if isinstance(value,dict) and value.get('error'):
            raise RuntimeError(value.get('message','Safari automation failed'))
        return value
    try:
        created=command('POST','/session',{'capabilities':{'alwaysMatch':{'browserName':'safari'}}})
    except RuntimeError as error:
        report={'passed':False,'blocked':True,'stage':'SafariDriver session creation','reason':str(error)}
        args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));raise SystemExit(2)
    session=created['sessionId'];prefix=f'/session/{session}'
    try:
        command('POST',prefix+'/timeouts',{'script':35000,'pageLoad':30000})
        # /ws without cookies is a JSON 401, not a browser Basic login prompt.
        command('POST',prefix+'/url',{'url':args.url.rstrip('/')+'/ws'})
        labels=dict(line.split(':',1) for line in args.auth_file.read_text().splitlines() if ':' in line)
        header='Basic '+base64.b64encode((labels['Username'].strip()+':'+labels['Password'].strip()).encode()).decode()
        script="""
const done=arguments[arguments.length-1], authorization=arguments[0];
(async()=>{
  const started=performance.now(),checks=[];
  const r=await fetch('/api/browser-session',{method:'POST',credentials:'same-origin',headers:{Authorization:authorization}});
  if(r.status!==200)throw Error('bootstrap HTTP '+r.status);
  const info=await r.json();if(info.authMode!=='cookie')throw Error('cookie bootstrap not active');
  checks.push('real_safari_http_bootstrap');
  if(document.cookie.includes('rtv_browser_session'))throw Error('HttpOnly cookie exposed to JS');
  checks.push('httponly_cookie_not_visible_to_javascript');
  const ws=new WebSocket(location.origin.replace('http','ws')+'/ws');
  await new Promise((resolve,reject)=>{
    const t=setTimeout(()=>reject(Error('cookie WS timeout')),15000);
    ws.onerror=()=>{clearTimeout(t);reject(Error('cookie WS failed'));};
    ws.onmessage=({data})=>{const m=JSON.parse(data);if(m.type==='session_event'&&m.event.eventType==='connection_opened'){clearTimeout(t);resolve();}};
  });
  ws.close();checks.push('real_safari_cookie_only_websocket');
  const expired=await fetch('/ws',{credentials:'omit'});
  if(expired.status!==401||expired.headers.has('WWW-Authenticate'))throw Error('Missing cookie not rejected safely');
  checks.push('no_login_cookie_no_basic_challenge');
  // Load the exact shipped connection module and repeat its bootstrap path.
  const code=await fetch('/connection.js',{headers:{Authorization:authorization}});
  if(!code.ok)throw Error('connection module unavailable');
  (0,eval)(await code.text());
  const attempt=new window.ConnectionAttempt();
  // Explicit HTTP header above uses the real gateway authentication path;
  // Safari's credential cache behavior is checked by this unmodified call.
  const next=await attempt.bootstrap();
  if(!next.ok)throw Error('repeat frontend bootstrap failed');
  checks.push('real_safari_frontend_bootstrap_repeat');
  done({passed:true,checks,elapsedMs:performance.now()-started,resources:info.resources,
        scope:'real_macOS_Safari_not_iPhone_device'});
})().catch(e=>done({passed:false,error:e.message}));
"""
        report=command('POST',prefix+'/execute/async',{'script':script,'args':[header]})
        report['browserVersion']=created.get('capabilities',{}).get('browserVersion')
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
        if not report.get('passed'):raise RuntimeError('Safari checks failed; retained report')
    finally:
        command('DELETE',prefix)


if __name__=='__main__':main()
