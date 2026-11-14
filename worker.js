import postgres from "postgres";

const DEFAULT_ORIGINS = ["https://www.mtahqq.dpdns.org", "https://mtahqq.dpdns.org"];
const CURATED_WEB = [
  {id:"mta-website",name:"MTA Apps Center",developer:"MTA Applications Team",summary:"Official MTA application discovery portal",description:"A web-based application store for discovering multi-platform software catalogs.",category:"portal",app_url:"https://www.mtahqq.dpdns.org",icon_url:"https://www.mtahqq.dpdns.org/icon.png",homepage:"https://www.mtahqq.dpdns.org"},
  {id:"firefox",name:"Mozilla Firefox",developer:"Mozilla Foundation",summary:"Free and open-source web browser",category:"browser",app_url:"https://www.mozilla.org/firefox",homepage:"https://www.mozilla.org"},
  {id:"libreoffice",name:"LibreOffice",developer:"The Document Foundation",summary:"Free office suite",category:"office",app_url:"https://www.libreoffice.org",homepage:"https://www.libreoffice.org"},
  {id:"gimp",name:"GIMP",developer:"GIMP Team",summary:"GNU Image Manipulation Program",category:"photo",app_url:"https://www.gimp.org",homepage:"https://www.gimp.org"},
  {id:"vivaldi",name:"Vivaldi",developer:"Vivaldi Technologies",summary:"Customizable web browser",category:"browser",app_url:"https://vivaldi.com",homepage:"https://vivaldi.com"},
  {id:"onlyoffice",name:"ONLYOFFICE",developer:"ONLYOFFICE",summary:"Office suite with collaboration",category:"office",app_url:"https://www.onlyoffice.com",homepage:"https://www.onlyoffice.com"},
  {id:"blender",name:"Blender",developer:"Blender Foundation",summary:"3D creation suite",category:"utilities",app_url:"https://www.blender.org",homepage:"https://www.blender.org"}
];
let cachedSql;
function database(env) {
  if (!env.HYPERDRIVE || !env.HYPERDRIVE.connectionString) throw new Error("Hyperdrive binding is unavailable");
  if (!cachedSql) cachedSql = postgres(env.HYPERDRIVE.connectionString, {max:5,idle_timeout:20,connect_timeout:10,fetch_types:false,prepare:false});
  return cachedSql;
}
function response(data,status,origin,extraHeaders={}) {
  return new Response(JSON.stringify(data),{status:status,headers:{"content-type":"application/json; charset=utf-8","access-control-allow-origin":origin,"access-control-allow-methods":"GET, POST, OPTIONS","access-control-allow-headers":"Content-Type, X-Admin-Token","vary":"Origin",...extraHeaders}});
}
function corsOrigin(request,env) {
  const allowed=(env.CORS_ORIGINS||DEFAULT_ORIGINS.join(",")).split(",").map(x=>x.trim()).filter(Boolean);
  const origin=request.headers.get("Origin")||"";
  if(!origin)return "*";
  if(allowed.includes(origin))return origin;
  // The catalogue GET endpoints expose public metadata only. Permit public website
  // origins so a stale CORS_ORIGINS variable cannot break the public marketplace.
  const requestedMethod=request.headers.get("Access-Control-Request-Method")||request.method;
  return requestedMethod==="GET"?"*":"null";
}
function safeHttps(value) {
  if(typeof value!=="string")return null;
  const s=value.trim();return s.startsWith("https://")?s:null;
}
function record(source,id,platform,name,sourceUrl,extra={}) {
  if(id==null||!String(id).trim()||!name||!safeHttps(sourceUrl))return null;
  return {source_key:source,external_id:String(id).trim(),platform:platform,name:String(name).trim().slice(0,500),
    developer:extra.developer??null,summary:extra.summary??null,description:extra.description??null,category:extra.category??null,
    license:extra.license??null,source_url:safeHttps(sourceUrl),app_url:safeHttps(extra.app_url),icon_url:safeHttps(extra.icon_url),metadata_json:extra.metadata_json||{}};
}
function unique(records) {
  const seen=new Set();return records.filter(r=>{if(!r)return false;const k=r.source_key+"|"+r.external_id;if(seen.has(k))return false;seen.add(k);return true;});
}
async function json(url) {
  const res=await fetch(url,{headers:{"user-agent":"MTA-Applications-Engine/0.3"}});
  if(!res.ok)throw new Error("HTTP "+res.status+" from "+new URL(url).hostname);
  return res.json();
}
async function fdroid() {
  const payload=await json("https://f-droid.org/repo/index-v2.json"),apps=payload.apps||payload.packages||{};
  const entries=Array.isArray(apps)?apps.map(x=>[null,x]):Object.entries(apps),out=[];
  for(const pair of entries){
    const key=pair[0],entry=pair[1];if(!entry||typeof entry!=="object")continue;
    const m=entry.metadata&&typeof entry.metadata==="object"?entry.metadata:entry,loc=m.localized||{},en=loc["en-US"]||loc.en||{};
    const val=(k)=>{let x=en[k]??loc[k]??m[k];while(x&&typeof x==="object"&&!Array.isArray(x))x=x["en-US"]??x.en??Object.values(x)[0];return x??null;};
    const id=key||entry.packageName||entry.package_name||entry.id,icon=val("icon"),iconName=typeof icon==="string"?icon:icon?.name,cats=m.categories||[];
    out.push(record("fdroid",id,"android",val("name")||val("title")||id,"https://f-droid.org/packages/"+id+"/",
      {developer:m.authorName||m.author,summary:val("summary")||val("short_description"),description:val("description")||val("full_description"),
       category:Array.isArray(cats)?cats.join(", "):cats,license:m.license,app_url:m.webSite||m.sourceCode,
       icon_url:iconName?"https://f-droid.org/repo/"+String(iconName).replace(/^\/+/, ""):null,
       metadata_json:{package_name:id,source_code:m.sourceCode,issue_tracker:m.issueTracker,website:m.webSite,added:m.added||entry.added,last_updated:m.lastUpdated||entry.lastUpdated}}));
  }
  return unique(out);
}
async function homebrew() {
  const payload=await json("https://formulae.brew.sh/api/cask.json"),items=Array.isArray(payload)?payload:(payload.casks||[]);
  return unique(items.map(x=>{const token=x.token,name=Array.isArray(x.name)?x.name[0]:token;return record("homebrew-cask",token,"macos",name,"https://formulae.brew.sh/cask/"+token,
    {summary:x.desc,description:x.desc,category:x.category,license:x.license,app_url:x.homepage,icon_url:x.artwork?.icon,metadata_json:{version:x.version,homepage:x.homepage,caveats:x.caveats}});}));
}
async function apple(env) {
  const terms=(env.APPLE_SEARCH_TERMS||"browser,office,photo,video,education,security,finance,communication,developer,utilities,messaging,maps,health,weather").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://itunes.apple.com/search");u.searchParams.set("term",term.trim());u.searchParams.set("country",env.APPLE_COUNTRY||"us");
    u.searchParams.set("limit",String(Math.max(1,Math.min(Number(env.APPLE_LIMIT||100),200))));u.searchParams.set("media","software");
    const p=await json(u.toString());
    for(const x of p.results||[]){if(x.kind&&!["software","software-package"].includes(x.kind))continue;
      out.push(record("apple-itunes",x.trackId,"ios",x.trackName,x.trackViewUrl,{developer:x.artistName,summary:x.shortDescription,description:x.description,
        category:x.genres?.[0],app_url:x.trackViewUrl,icon_url:x.artworkUrl512||x.artworkUrl100,
        metadata_json:{bundle_id:x.bundleId,version:x.version,price:x.price,genres:x.genres}}));}
  }catch(e){console.log("Apple search failed",term,String(e));}}
  return unique(out);
}
async function winget(env) {
  const terms=(env.WINGET_SEARCH_TERMS||"browser,office,photo,video,education,security,finance,communication,developer,utilities").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://api.winget.run/v2/packages");u.searchParams.set("query",term.trim());
    u.searchParams.set("take",String(Math.max(1,Math.min(Number(env.WINGET_LIMIT||24),100))));u.searchParams.set("page","0");
    const p=await json(u.toString()),items=Array.isArray(p)?p:(p.Packages||p.packages||p.value||[]);
    for(const x of items){const latest=x.Latest||x.latest||{},id=x.Id||x.id,name=latest.Name||x.name||id;
      out.push(record("winget-run-community",id,"windows",name,id?"https://winget.run/pkg/"+id:null,{developer:latest.Publisher||x.publisher,
        summary:latest.Description||x.description,description:latest.Description||x.description,license:latest.License||x.license,
        app_url:latest.Homepage||x.homePageUrl,icon_url:x.IconUrl||x.iconUrl,metadata_json:{version:latest.Version||x.version,tags:latest.Tags||x.tags}}));}
  }catch(e){console.log("WinGet search failed",term,String(e));}}
  return unique(out);
}
async function flathub(env) {
  try{
    const p=await json("https://flathub.org/api/v2/appstream?filter=apps&sort=alphabetical");
    let items=Array.isArray(p)?p:(p.items||p.apps||p.data||p.results||[]);
    if(items&&!Array.isArray(items)&&typeof items==="object")items=Object.entries(items).map(([id,v])=>Object.assign({id:id},v||{}));
    const ids=Array.from(new Set(items.map(x=>typeof x==="string"?x:(x.id||x.app_id||x.appId||x.flatpak_id)).filter(Boolean))).slice(0,Math.max(1,Math.min(Number(env.FLATHUB_MAX_APPS||1000),5000)));
    const out=[];
    for(let i=0;i<ids.length;i+=8){
      const batch=await Promise.all(ids.slice(i,i+8).map(async id=>{try{
        const d=await json("https://flathub.org/api/v2/appstream/"+encodeURIComponent(id)+"?locale=en"),cats=d.categories||[],shots=d.screenshots||[];
        return record("flathub",id,"linux",d.name||id,"https://flathub.org/apps/"+id,{developer:d.developer_name||d.developer,
          summary:d.summary,description:d.description,category:Array.isArray(cats)?cats.join(", "):cats,license:d.license,app_url:d.project_url,
          icon_url:shots[0]?.url,metadata_json:{flatpak_id:id,categories:cats}});
      }catch(e){return null;}}));out.push(...batch);
    }return unique(out);
  }catch(e){console.log("Flathub fetch failed",String(e));return [];}
}
async function curated() {
  return unique(CURATED_WEB.map(x=>record("mta-curated-web",x.id||x.name,"web",x.name,x.app_url||x.homepage,
    {developer:x.developer,summary:x.summary,description:x.description,category:x.category,app_url:x.app_url||x.homepage,
     icon_url:x.icon_url,metadata_json:{homepage:x.homepage,curated:true}})));
}
async function upsert(db,records) {
  const counts={inserted:0,updated:0,unchanged:0,skipped:0};
  const columns=["platform","name","developer","summary","description","category","license","source_url","app_url","icon_url"];
  for(const r of records){
    if(!r?.source_key||!r.external_id||!r.name||!r.source_url){counts.skipped++;continue;}
    const values=[r.platform,r.name,r.developer,r.summary,r.description,r.category,r.license,r.source_url,r.app_url,r.icon_url];
    const found=await db.unsafe("SELECT id,platform,name,developer,summary,description,category,license,source_url,app_url,icon_url,metadata FROM apps WHERE source_key=$1 AND external_id=$2 LIMIT 1",[r.source_key,r.external_id]);
    if(found.length){
      const old=found[0],same=columns.every((key,i)=>(old[key]??null)===(values[i]??null))&&JSON.stringify(old.metadata||{})===JSON.stringify(r.metadata_json||{});
      if(same){counts.unchanged++;continue;}
      await db.unsafe("UPDATE apps SET platform=$1,name=$2,developer=$3,summary=$4,description=$5,category=$6,license=$7,source_url=$8,app_url=$9,icon_url=$10,metadata=$11::json,updated_at=now() WHERE id=$12",
        [...values,JSON.stringify(r.metadata_json||{}),old.id]);
      counts.updated++;
    }else{
      await db.unsafe("INSERT INTO apps (source_key,external_id,platform,name,developer,summary,description,category,license,source_url,app_url,icon_url,metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::json) ON CONFLICT (source_key,external_id) DO NOTHING",
        [r.source_key,r.external_id,...values.slice(0,10),JSON.stringify(r.metadata_json||{})]);
      counts.inserted++;
    }
  }
  return counts;
}
function isAdmin(request,env) {
  const expected=env.ADMIN_API_TOKEN||"",supplied=request.headers.get("X-Admin-Token")||"";
  if(!expected||!supplied||expected.length!==supplied.length)return false;
  let diff=0;for(let i=0;i<expected.length;i++)diff|=expected.charCodeAt(i)^supplied.charCodeAt(i);return diff===0;
}
async function handle(request,env) {
  const origin=corsOrigin(request,env);
  if(request.method==="OPTIONS")return new Response(null,{status:204,headers:{"access-control-allow-origin":origin,"access-control-allow-methods":"GET, POST, OPTIONS","access-control-allow-headers":"Content-Type, X-Admin-Token","vary":"Origin"}});
  const url=new URL(request.url),path=url.pathname;
  if(path==="/health"&&request.method==="GET")return response({status:"ok",service:"MTA Applications Engine",version:"0.3.0"},200,origin);
  if(path==="/api/v1/apps"&&request.method==="GET"){
    const limit=Math.max(1,Math.min(Number(url.searchParams.get("limit")||50),200)),offset=Math.max(0,Number(url.searchParams.get("offset")||0));
    const clauses=[],quote=value=>"'"+String(value).replace(/'/g,"''")+"'";
    const q=url.searchParams.get("q"),platform=url.searchParams.get("platform"),source=url.searchParams.get("source");
    if(q){const term=quote("%"+q.trim()+"%");clauses.push("(name ILIKE "+term+" OR developer ILIKE "+term+" OR summary ILIKE "+term+" OR description ILIKE "+term+" OR category ILIKE "+term+")");}
    if(platform)clauses.push("platform = "+quote(platform.toLowerCase()));
    if(source)clauses.push("source_key = "+quote(source.toLowerCase()));
    const where=clauses.length?"WHERE "+clauses.join(" AND ")+" ":"";
    const db=database(env);
    const totalRows=await db.unsafe("SELECT count(*)::int AS total FROM apps "+where);
    const sql="SELECT id,source_key AS source,external_id,platform,name,developer,summary,description,category,license,source_url,app_url,icon_url FROM apps "+where+"ORDER BY name,platform,source_key,external_id LIMIT "+limit+" OFFSET "+offset;
    const rows=await db.unsafe(sql);
    return response(rows,200,origin,{"access-control-expose-headers":"X-Total-Count","x-total-count":String(totalRows[0]?.total||0)});
  }
  if(path==="/api/v1/catalog-stats"&&request.method==="GET"){
    const db=database(env),results=await Promise.all([
      db.unsafe("SELECT count(*)::int AS total FROM apps"),
      db.unsafe("SELECT platform,count(*)::int AS count FROM apps GROUP BY platform ORDER BY platform"),
      db.unsafe("SELECT source_key,count(*)::int AS count FROM apps GROUP BY source_key ORDER BY source_key")
    ]);
    return response({total:results[0][0]?.total||0,platforms:Object.fromEntries(results[1].map(x=>[x.platform,x.count])),
      sources:Object.fromEntries(results[2].map(x=>[x.source_key,x.count])),metadata_only:true},200,origin);
  }
  if((path==="/api/v1/ingest/catalogs"||path==="/api/v1/ingest/fdroid")&&request.method==="POST"){
    if(!isAdmin(request,env))return response({detail:"Ingestion is disabled or unauthorized"},403,origin);
    const db=database(env);
    if(path==="/api/v1/ingest/fdroid"){
      let records;try{records=await fdroid();}catch(e){return response({detail:"F-Droid catalogue unavailable"},502,origin);}
      if(!records.length)return response({detail:"F-Droid returned no usable application metadata"},502,origin);
      return response({source:"F-Droid",discovered:records.length,...await upsert(db,records),note:"Metadata only. No APK files were downloaded, mirrored, or hosted."},200,origin);
    }
    const tasks={fdroid:fdroid(),flathub:flathub(env),"homebrew-cask":homebrew(),"apple-itunes":apple(env),"winget-run-community":winget(env),"mta-curated-web":curated()};
    const entries=await Promise.all(Object.entries(tasks).map(async pair=>[pair[0],await pair[1].catch(e=>{console.log("Catalogue failed",pair[0],String(e));return []})]));
    const summary={};
    for(const entry of entries){const key=entry[0],records=entry[1];try{summary[key]=Object.assign({fetched:records.length},await upsert(db,records));}catch(e){console.log("Database write failed",key,String(e));summary[key]={fetched:records.length,error:"Database write failed"};}}
    return response({status:"completed",sources:summary,note:"Metadata only. No installer files were downloaded, mirrored, or hosted."},200,origin);
  }
  return response({detail:"Not Found"},404,origin);
}
export default {async fetch(request,env){try{return await handle(request,env);}catch(error){console.error("MTA Applications Engine request failed",error);return response({detail:"Internal server error"},500,corsOrigin(request,env));}}};
