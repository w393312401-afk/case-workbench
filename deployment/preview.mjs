import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve, extname, sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import worker from './site/_worker.js';

const root = fileURLToPath(new URL('./site/', import.meta.url));
const types = {'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8',
  '.css':'text/css; charset=utf-8','.json':'application/json; charset=utf-8','.txt':'text/plain; charset=utf-8'};
const env = {ASSETS:{async fetch(request) {
  const url = new URL(typeof request === 'string' ? request : request.url);
  const relative = decodeURIComponent(url.pathname) === '/' ? 'index.html' : decodeURIComponent(url.pathname).slice(1);
  const path = resolve(root, relative);
  if (!path.startsWith(resolve(root) + sep)) return new Response('Not found', {status:404});
  try {return new Response(await readFile(path), {headers:{'content-type':types[extname(path)] || 'text/plain'}});}
  catch {return new Response('Not found', {status:404});}
}}};
http.createServer(async (req,res) => {
  try {
    const request = new Request('http://127.0.0.1:8798' + req.url, {method:req.method,headers:req.headers});
    const result = await worker.fetch(request, env, {waitUntil(){}});
    res.writeHead(result.status, Object.fromEntries(result.headers));
    if (req.method === 'HEAD') res.end();
    else res.end(Buffer.from(await result.arrayBuffer()));
  } catch (error) {console.error(error);res.writeHead(500);res.end('Preview error');}
}).listen(8798, '127.0.0.1', () => console.log('Text site preview: http://127.0.0.1:8798'));
