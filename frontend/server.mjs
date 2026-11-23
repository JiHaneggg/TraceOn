// Local frontend dev server: fixed API fixtures by default; explicit API proxy.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createDemoApi,ApiError} from './mock-api.mjs';

const root=fileURLToPath(new URL('.',import.meta.url));
export async function frontendServer({api=null,historyFile=resolve(root,'.local/history.json')}={}){
  const upstream=api?new URL(api):null;
  if(upstream&&(!['http:','https:'].includes(upstream.protocol)||upstream.username||upstream.password||upstream.search||upstream.hash||upstream.pathname!=='/'))throw Error('API must be an HTTP(S) origin without credentials');
  const mock=upstream?null:await createDemoApi({historyFile});
  return createServer(async(req,res)=>{
    function send(status,data,mime='application/json',extra={}){
      res.writeHead(status,{'Content-Type':mime+'; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'",...extra});
      res.end(typeof data==='string'||Buffer.isBuffer(data)?data:JSON.stringify(data));
    }
    try{
      const host=req.headers.host,hostUrl=new URL('http://'+host);
      if(!['127.0.0.1','localhost','[::1]'].includes(hostUrl.hostname)||Number(hostUrl.port)!==req.socket.localPort)return send(421,{error:'invalid_host'});
      if(req.headers['sec-fetch-site']==='cross-site'||req.headers.origin&&req.headers.origin!==`http://${host}`)return send(403,{error:'same_origin_required'});
      const url=new URL(req.url,`http://${host}`);
      if(url.pathname==='/_frontend/config'&&req.method==='GET')return send(200,{mode:upstream?'api':'fixture'});
      if(url.pathname.startsWith('/api/')){
        if(!['GET','POST'].includes(req.method))return send(405,{error:'method_not_allowed'});
        let raw='';for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>2048)return send(413,{error:'body_size'});}
        if(req.method==='POST'&&!req.headers['content-type']?.startsWith('application/json'))return send(400,{error:'invalid_body'});
        if(upstream){
          try{
            const response=await fetch(new URL(url.pathname+url.search,upstream),{method:req.method,headers:raw?{'Content-Type':'application/json'}:{},body:raw||undefined,signal:AbortSignal.timeout(35000),redirect:'error'});
            const mime=response.headers.get('content-type')?.split(';')[0]||'application/json';
            return send(response.status,Buffer.from(await response.arrayBuffer()),mime,response.headers.has('content-disposition')?{'Content-Disposition':response.headers.get('content-disposition')}:{});
          }catch{return send(503,{error:'backend_unavailable',message:'백엔드에 연결하지 못했습니다. 연결 상태를 확인하고 다시 조회해 주세요.'});}
        }
        let body;try{body=raw?JSON.parse(raw):undefined;}catch{return send(400,{error:'invalid_body',message:'JSON 요청을 확인해 주세요.'});}
        const result=await mock(req.method,req.url,body);
        return result.download?send(200,result.content,result.mime,{'Content-Disposition':`attachment; filename="traceon-fixture.${result.extension}"`}):send(200,result);
      }
      if(req.method!=='GET')return send(405,{error:'method_not_allowed'});
      const path=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname).slice(1);
      // Only shipped frontend assets. Fixtures and saved captures are never static URLs.
      const allowed=/^(?:[a-z-]+\.(?:html|css|mjs)|vendor\/(?:cytoscape\.min\.js|CYTOSCAPE-LICENSE))$/;
      if(!allowed.test(path))return send(404,{error:'not_found'});
      try{const raw=await readFile(resolve(root,'static',path));return send(200,raw,{'.html':'text/html','.css':'text/css','.mjs':'text/javascript','.js':'text/javascript'}[extname(path)]||'text/plain');}
      catch{return send(404,{error:'not_found'});}
    }catch(e){return send(e instanceof ApiError?e.status:500,{error:e instanceof ApiError?e.code:'unavailable',message:e instanceof ApiError?e.message:'프론트 시연 요청을 완료하지 못했습니다.'});}
  });
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const value=flag=>{const i=process.argv.indexOf(flag);return i<0?null:process.argv[i+1];};
  const port=Number(value('--port')||4173);if(!Number.isInteger(port)||port<1||port>65535)throw Error('Invalid port');
  const api=value('--api'),server=await frontendServer({api,historyFile:value('--history-file')||undefined});
  server.listen(port,'127.0.0.1',()=>console.log(`TraceOn frontend: http://127.0.0.1:${port} (${api?'API proxy':'fixed synthetic fixtures'})`));
}
